import re
import threading
import time
import uuid
import shlex
import os
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, current_app, send_from_directory, Response
)
from urllib.parse import urlparse, urljoin
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from .database import load_db, save_db
import json
from .tasks import tasks, execute_inference_task
from .s3_utils import generate_presigned_url, get_s3_config, get_public_s3_url
from .utils import predict_output_filename

main_bp = Blueprint('main', __name__)

@main_bp.route('/robots.txt')
def robots_txt():
    return send_from_directory(current_app.static_folder, 'robots.txt')

@main_bp.route('/sw.js')
def sw_js():
    return send_from_directory(current_app.static_folder, 'sw.js')

@main_bp.route('/ads.txt')
def ads_txt():
    return send_from_directory(current_app.static_folder, 'ads.txt')

@main_bp.route('/firebase-messaging-sw.js')
def firebase_messaging_sw():
    return send_from_directory(current_app.static_folder, 'firebase-messaging-sw.js')

@main_bp.route('/set-language/<lang_code>')
def set_language(lang_code):
    if lang_code in ['zh', 'en']:
        session['locale'] = lang_code
    else:
        session['locale'] = 'en' # Default fallback

    # Safe redirect
    target = request.referrer
    if not target or urlparse(target).netloc != urlparse(request.host_url).netloc:
        target = url_for('main.index')

    return redirect(target)

@main_bp.route('/sitemap.xml')
def sitemap():
    db = load_db()
    server_domain = db.get('settings', {}).get('server_domain', request.url_root.rstrip('/'))
    today = datetime.now().strftime('%Y-%m-%d')

    def external_url_for(endpoint, **values):
        if '127.0.0.1' in server_domain or 'localhost' in server_domain:
            return url_for(endpoint, **values, _external=True)
        return f"{server_domain}{url_for(endpoint, **values)}"

    # Static pages with priority and change frequency
    static_urls = [
        {'loc': external_url_for('main.index'), 'lastmod': today, 'changefreq': 'daily', 'priority': '1.0'},
        {'loc': external_url_for('auth.login'), 'lastmod': today, 'changefreq': 'monthly', 'priority': '0.5'},
        {'loc': external_url_for('auth.register'), 'lastmod': today, 'changefreq': 'monthly', 'priority': '0.5'},
        {'loc': external_url_for('main.article_list'), 'lastmod': today, 'changefreq': 'weekly', 'priority': '0.8'},
    ]

    # Dynamic pages from "spaces"
    spaces = db.get('spaces', {}).values()
    space_urls = []
    for space in spaces:
        last_mod_date = today
        if 'last_modified' in space:
            try:
                dt = datetime.fromisoformat(space['last_modified'])
                last_mod_date = dt.strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                pass
        
        # Add image data for spaces with covers
        images = []
        if space.get('cover_image'):
            cover_url = space['cover_image']
            if not cover_url.startswith('http'):
                cover_url = f"{server_domain}{cover_url}"
            images.append({
                'loc': cover_url,
                'caption': space.get('name', 'AI Project'),
                'title': space.get('description', space.get('name', 'AI Project'))[:100]
            })
        
        # Add language alternates
        alternates = [
            {'hreflang': 'zh', 'href': external_url_for('main.ai_project_view', ai_project_id=space['id']) + '?lang=zh'},
            {'hreflang': 'en', 'href': external_url_for('main.ai_project_view', ai_project_id=space['id']) + '?lang=en'},
            {'hreflang': 'x-default', 'href': external_url_for('main.ai_project_view', ai_project_id=space['id'])}
        ]
        
        url_data = {
            'loc': external_url_for('main.ai_project_view', ai_project_id=space['id']),
            'lastmod': last_mod_date,
            'changefreq': 'weekly',
            'priority': '0.9',
            'images': images,
            'alternates': alternates
        }
        space_urls.append(url_data)

    # Dynamic pages from "articles"
    articles = db.get('articles', {}).values()
    article_urls = []
    for article in articles:
        last_mod_date = today
        if 'updated_at' in article:
            try:
                dt = datetime.fromisoformat(article['updated_at'])
                last_mod_date = dt.strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                pass
        url_data = {
            'loc': external_url_for('main.article_view', slug=article['slug']),
            'lastmod': last_mod_date,
            'changefreq': 'monthly',
            'priority': '0.7'
        }
        article_urls.append(url_data)

    all_urls = static_urls + space_urls + article_urls
    sitemap_xml = render_template('sitemap_template.xml', urls=all_urls)
    return Response(sitemap_xml, mimetype='application/xml')

@main_bp.route('/')
def index():
    db = load_db()
    ai_projects_list = sorted(db["spaces"].values(), key=lambda x: x["name"])
    username = session.get('username')

    # Add 'is_liked' status to each project for the current user
    for project in ai_projects_list:
        if username and 'liked_by' in project and username in project['liked_by']:
            project['is_liked'] = True
        else:
            project['is_liked'] = False

    categories = db.get('categories', [])
    announcement = db.get('announcement', {})

    # --- Hreflang links generation ---
    server_domain = db.get('settings', {}).get('server_domain', request.url_root.rstrip('/'))
    def external_url_for(endpoint, **values):
        if '127.0.0.1' in server_domain or 'localhost' in server_domain:
             return url_for(endpoint, **values, _external=True)
        return f"{server_domain}{url_for(endpoint, **values)}"

    # Note: The 'en' URL is hypothetical. It assumes you will have a mechanism
    # to serve English content based on a 'lang' query parameter or similar.
    hreflang_links = [
        {'lang': 'zh', 'url': external_url_for('main.index')},
        {'lang': 'en', 'url': external_url_for('main.index', lang='en')},
        {'lang': 'x-default', 'url': external_url_for('main.index')}
    ]

    # Find the latest message from an admin
    latest_admin_message = None
    chat_messages = db.get('chat_messages', [])
    admin_usernames = [u for u, d in db.get('users', {}).items() if d.get('is_admin')]

    for message in reversed(chat_messages):
        if message.get('username') in admin_usernames:
            latest_admin_message = message
            break

    banner = db.get('banner', {})

    return render_template(
        "index.html",
        ai_projects=ai_projects_list,
        announcement=announcement,
        categories=categories,
        hreflang_links=hreflang_links,
        latest_admin_message=latest_admin_message,
        banner=banner
    )


@main_bp.route('/profile')
def profile():
    if not session.get('logged_in'):
        return redirect(url_for('auth.login'))

    db = load_db()
    user_data = db['users'].get(session['username'], {})
    api_key = user_data.get('api_key', '未找到 API 密钥')
    if not session.get('is_admin') and api_key and len(api_key) > 8:
        api_key = f"{api_key[:4]}...{api_key[-4:]}"
    settings = db.get('settings', {})
    pro_settings = db.get('pro_settings', {})
    pro_plans = db.get('pro_plans')
    if not isinstance(pro_plans, list):
        pro_plans = []

    # Get today's date string in Beijing time on the server
    beijing_tz = ZoneInfo("Asia/Shanghai")
    today_str = datetime.now(beijing_tz).strftime('%Y-%m-%d')

    return render_template('profile.html', user=user_data, api_key=api_key, settings=settings, pro_settings=pro_settings, pro_plans=pro_plans, today_str=today_str)

@main_bp.route('/bind_email', methods=['POST'])
def bind_email():
    if not session.get('logged_in'):
        return jsonify({'success': False, 'error': '请先登录'}), 401

    username = session['username']
    email = request.form.get('email', '').strip()

    if not email:
        return jsonify({'success': False, 'error': '邮箱不能为空'}), 400

    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({'success': False, 'error': '邮箱格式不正确'}), 400

    db = load_db()

    # Check if this email is already used by ANOTHER user
    for u, u_data in db['users'].items():
        if u != username and u_data.get('email') == email:
            return jsonify({'success': False, 'error': '该邮箱已被其他账号绑定，请使用其他邮箱。'}), 400

    user = db['users'].get(username)
    if not user:
         return jsonify({'success': False, 'error': '用户未找到'}), 404

    # Update email
    user['email'] = email
    save_db(db)

    return jsonify({'success': True, 'message': '邮箱绑定成功'})


@main_bp.route('/check-in', methods=['POST'])
def check_in():
    if not session.get('logged_in'):
        return jsonify({'success': False, 'error': '请先登录'}), 401

    username = session['username']
    db = load_db()
    user = db['users'].get(username)

    if not user:
        return jsonify({'success': False, 'error': '未找到用户'}), 404

    # Use Beijing Time (UTC+8)
    beijing_tz = ZoneInfo("Asia/Shanghai")
    today_str = datetime.now(beijing_tz).strftime('%Y-%m-%d')

    if user.get('last_check_in_date') == today_str:
        return jsonify({'success': False, 'error': '您今天已经签到过了'}), 400

    user['last_check_in_date'] = today_str
    check_in_history = user.get('check_in_history')
    if not isinstance(check_in_history, list):
        check_in_history = []
    if today_str not in check_in_history:
        check_in_history.append(today_str)
    user['check_in_history'] = check_in_history

    save_db(db)

    return jsonify({'success': True, 'message': '签到成功！'})


@main_bp.route('/cloud-terminal')
def cloud_terminal():
    # Define candidate endpoints
    # These are the standard app names we expect.
    candidate_apps = ['cloud-terminal', 'cloud-terminal-gpu']
    db = load_db()
    project_id = current_app.config.get('CEREBRIUM_PROJECT_ID')

    if not project_id:
        # Fallback: Try to get project_id from settings
        project_id = db.get('settings', {}).get('cerebrium_project_id')

    # Fallback if project_id is not set: attempt to extract from a known URL structure if available
    # or rely on what the user might have provided in env vars
    if not project_id:
         # If no project ID, we can't construct URLs to probe, so we can't detect anything.
         # However, the user's example had 'p-d0cdeab4'.
         pass

    # Construct potential URLs
    potential_targets = []
    if project_id:
        for app_name in candidate_apps:
            url = f"https://api.aws.us-east-1.cerebrium.ai/v4/{project_id}/{app_name}/run"
            potential_targets.append({'name': app_name, 'url': url})
    else:
        # Fallback: if we have URLs in config (which we shouldn't rely on as per new requirement,
        # but if project_id is missing, maybe we can try to use them if they exist)
        # Actually, the requirement is "run environment do not preset".
        # So if we don't have a project ID, we essentially find nothing.
        # But let's check if the old config vars are still there, maybe we can extract project_id from them.
        # (I removed the defaults from config.py but the env vars might still be there)
        cpu_url = current_app.config.get('CEREBRIUM_CLOUD_TERMINAL_CPU_URL')
        if cpu_url and '/v4/' in cpu_url:
            try:
                parts = cpu_url.split('/v4/')[1].split('/')
                if parts:
                    project_id = parts[0]
                    # Now retry building targets
                    for app_name in candidate_apps:
                        url = f"https://api.aws.us-east-1.cerebrium.ai/v4/{project_id}/{app_name}/run"
                        potential_targets.append({'name': app_name, 'url': url})
            except Exception:
                pass

    # Server-side detection
    terminal_targets = []
    import requests

    # We use a short timeout to avoid blocking the page load for too long
    detection_timeout = 1.0

    # If we have a project ID, we probe the standard endpoints
    if project_id:
        for app_name in candidate_apps:
            url = f"https://api.aws.us-east-1.cerebrium.ai/v4/{project_id}/{app_name}/run"
            try:
                # Probe the endpoint to see if it exists
                response = requests.post(url, json={}, timeout=detection_timeout)
                if response.status_code != 404:
                    terminal_targets.append({'name': app_name, 'url': url})
            except requests.RequestException:
                pass
    else:
        # Fallback to what was found in potential_targets if project_id wasn't explicitly set but inferred
        for target in potential_targets:
            try:
                response = requests.post(target['url'], json={}, timeout=detection_timeout)
                if response.status_code != 404:
                    terminal_targets.append(target)
            except requests.RequestException:
                pass

    # Add user's personal Cerebrium configs if logged in
    if session.get('logged_in'):
        username = session.get('username')
        user = db.get('users', {}).get(username)
        if user:
            user_configs = user.get('cerebrium_configs', [])
            for cfg in user_configs:
                cfg_name = cfg.get('name', 'Unnamed')
                terminal_targets.append({
                    'name': cfg_name,
                    'description': '用户自配置'
                })

    quick_commands = [
        {'label': '列出目录', 'command': 'ls -la'},
        {'label': '查看环境变量', 'command': 'env | head'}
    ]
    hardware_presets = [
        {
            'key': 'cpu',
            'label': 'CPU (2 vCPU)',
            'description': '经济实惠，适合轻量任务',
            'default_app_name': 'cloud-terminal'
        },
        {
            'key': 'l40s',
            'label': 'L40S (24GB)',
            'description': '低成本 GPU，适合实时部署',
            'default_app_name': 'cloud-terminal'
        },
        {
            'key': 'h100',
            'label': 'H100 (80GB)',
            'description': '最高性能 GPU，适合重度任务',
            'default_app_name': 'cloud-terminal'
        }
    ]
    # Determine if we have a GPU endpoint detected
    has_gpu_endpoint = any('gpu' in t['name'] for t in terminal_targets)

    display_targets = [{'name': target['name'], 'description': target.get('description', '自动探测')} for target in terminal_targets]

    terminal_announcement = db.get('terminal_announcement', {})

    return render_template(
        'cloud_terminal.html',
        quick_commands=quick_commands,
        terminal_targets=display_targets,
        hardware_presets=hardware_presets,
        has_gpu_endpoint=has_gpu_endpoint,
        terminal_announcement=terminal_announcement
    )

@main_bp.route('/change_password', methods=['POST'])
def change_password():
    if not session.get('logged_in'):
        return redirect(url_for('auth.login'))

    username = session['username']
    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')

    if not all([current_password, new_password, confirm_password]):
        flash('所有字段都是必填的。', 'error')
        return redirect(url_for('main.profile'))

    if new_password != confirm_password:
        flash('新密码和确认密码不匹配。', 'error')
        return redirect(url_for('main.profile'))

    db = load_db()
    user = db['users'].get(username, {})

    if not user:
        flash('未找到用户。', 'error')
        return redirect(url_for('auth.logout'))

    if not check_password_hash(user.get('password_hash', ''), current_password):
        flash('当前密码不正确。', 'error')
        return redirect(url_for('main.profile'))

    user['password_hash'] = generate_password_hash(new_password)
    save_db(db)

    flash('密码已成功更新。', 'success')
    return redirect(url_for('main.profile'))

@main_bp.route('/change_username', methods=['POST'])
def change_username():
    if not session.get('logged_in'):
        return jsonify({'success': False, 'error': '请先登录'}), 401

    old_username = session['username']
    new_username = request.form.get('new_username', '').strip()

    if not new_username:
        return jsonify({'success': False, 'error': '用户名不能为空'}), 400

    if len(new_username) < 3 or len(new_username) > 20:
        return jsonify({'success': False, 'error': '用户名长度必须在3-20个字符之间'}), 400

    # Basic validation: alphanumeric and underscore only
    if not re.match(r'^[a-zA-Z0-9_]+$', new_username):
        return jsonify({'success': False, 'error': '用户名只能包含字母、数字和下划线'}), 400

    if new_username == old_username:
        return jsonify({'success': False, 'error': '新用户名不能与旧用户名相同'}), 400

    db = load_db()

    # Check if new username exists
    if new_username in db['users']:
        return jsonify({'success': False, 'error': '用户名已被占用'}), 400

    user_data = db['users'].get(old_username)
    if not user_data:
        return jsonify({'success': False, 'error': '用户未找到'}), 404

    # Check cooldown (30 days)
    last_change = user_data.get('last_username_change_date')
    now = datetime.utcnow()

    if last_change:
        last_change_dt = datetime.fromisoformat(last_change)
        if (now - last_change_dt).days < 30:
            days_left = 30 - (now - last_change_dt).days
            return jsonify({'success': False, 'error': f'您最近修改过用户名，请在 {days_left} 天后再试。'}), 400

    try:
        # Perform Rename Transaction
        # 1. Update user record
        user_data['username'] = new_username
        user_data['last_username_change_date'] = now.isoformat()

        # Ensure S3 folder mapping is preserved
        if 's3_folder_name' not in user_data:
            user_data['s3_folder_name'] = old_username

        # 2. Move user entry in DB
        db['users'][new_username] = user_data
        del db['users'][old_username]

        # 3. Update References

        # Chat Messages
        for msg in db.get('chat_messages', []):
            if msg.get('username') == old_username:
                msg['username'] = new_username

        # Chat History
        for msg in db.get('chat_history', []):
            if msg.get('username') == old_username:
                msg['username'] = new_username

        # Uploaded Files
        for file_info in db.get('uploaded_files', {}).values():
            if file_info.get('username') == old_username:
                file_info['username'] = new_username

        # Spaces (Likes)
        for space in db.get('spaces', {}).values():
            if 'liked_by' in space and old_username in space['liked_by']:
                space['liked_by'].remove(old_username)
                space['liked_by'].append(new_username)

        # User States
        if old_username in db.get('user_states', {}):
            db['user_states'][new_username] = db['user_states'].pop(old_username)

        # Daily Active Users
        for date_key, users_list in db.get('daily_active_users', {}).items():
            if old_username in users_list:
                users_list.remove(old_username)
                users_list.append(new_username)

        # Invitation Codes (generated by user)
        # Assuming prefix logic might be used elsewhere, but DB stores exact keys.
        # If code generation uses username in key, those keys are now stale but valid objects.
        # We won't rename keys for invitation codes to avoid breaking shared links,
        # but we might need to update metadata if it exists.

        # Update Session
        session['username'] = new_username

        save_db(db)
        return jsonify({'success': True, 'message': '用户名修改成功', 'new_username': new_username})

    except Exception as e:
        return jsonify({'success': False, 'error': f'修改失败: {str(e)}'}), 500

@main_bp.route('/delete_account', methods=['POST'])
def delete_account():
    if not session.get('logged_in'):
        return redirect(url_for('auth.login'))

    username = session['username']
    if username == 'admin':
        flash('管理员账户不能被删除。', 'error')
        return redirect(url_for('main.profile'))

    db = load_db()

    # 1. Delete user's results directory
    user_dir = os.path.join(current_app.instance_path, current_app.config['RESULTS_FOLDER'], username)
    if os.path.exists(user_dir):
        shutil.rmtree(user_dir)

    # 2. Remove user's likes from all spaces
    for space in db.get('spaces', {}).values():
        if 'liked_by' in space and username in space['liked_by']:
            space['liked_by'].remove(username)

    # 4. Remove user's state
    if username in db.get('user_states', {}):
        del db['user_states'][username]

    # 5. Remove user's generated invitation codes
    generated_code_prefix = f"ldo-{username}-"
    codes_to_delete = [code for code in db.get('invitation_codes', {}) if code.startswith(generated_code_prefix)]
    for code in codes_to_delete:
        del db['invitation_codes'][code]

    # 6. Delete the user object itself
    if username in db['users']:
        del db['users'][username]

    save_db(db)

    # 7. Log the user out
    session.clear()
    flash('您的账户和所有相关数据已被成功删除。', 'success')
    return redirect(url_for('auth.login'))

@main_bp.route('/favorites')
def favorites():
    if not session.get('logged_in'):
        return redirect(url_for('auth.login'))

    db = load_db()
    username = session['username']

    all_spaces = db.get('spaces', {}).values()
    liked_spaces = [space for space in all_spaces if username in space.get('liked_by', [])]

    # Add is_liked status for the template
    for space in liked_spaces:
        space['is_liked'] = True

    return render_template('favorites.html', ai_projects=liked_spaces)

@main_bp.route('/settings', methods=['GET', 'POST'])
def settings():
    if not session.get('logged_in'):
        return redirect(url_for('auth.login'))

    db = load_db()

    if request.method == 'POST':
        if session.get('is_admin'):
            db['settings']['server_domain'] = request.form['server_domain'].rstrip('/')
            save_db(db)
            flash('设置已保存!', 'success')
        else:
            flash('只有管理员可以修改设置。', 'error')
        return redirect(url_for('main.settings'))

    settings = db.get('settings', {})
    return render_template('settings.html', settings=settings)


@main_bp.route("/ai_project/<ai_project_id>")
def ai_project_view(ai_project_id):
    db = load_db()
    ai_project = db["spaces"].get(ai_project_id)
    if not ai_project:
        return "AI Project not found", 404

    # Allow public access but prepare user-specific data if logged in
    username = session.get("username")
    user_data = {}
    api_key = "YOUR_API_KEY_PLACEHOLDER"
    user_has_invitation_code = False
    is_waiting_for_file = False
    selected_file_key = None
    custom_gpu_configs = []
    last_cerebrium_result = None

    DEFAULT_DOMAIN_PLACEHOLDER = 'https://pumpkinai.it.com'
    raw_server_domain = (db.get('settings', {}).get('server_domain') or '').rstrip('/')
    current_request_domain = (request.url_root or '').rstrip('/')
    effective_server_domain = raw_server_domain if raw_server_domain and raw_server_domain != DEFAULT_DOMAIN_PLACEHOLDER else (current_request_domain or raw_server_domain)

    space_card_type = ai_project.get('card_type', 'standard')
    cerebrium_timeout_seconds = ai_project.get('cerebrium_timeout_seconds', 300) or 300

    announcement = db.get('announcement', {})

    if username:
        user_data = db["users"].get(username, {})
        api_key = user_data.get("api_key") or "YOUR_API_KEY_PLACEHOLDER"
        user_has_invitation_code = user_data.get("has_invitation_code", False)
        user_state = db.get("user_states", {}).get(username, {})
        is_waiting_for_file = user_state.get("is_waiting_for_file", False)
        selected_file_key = user_state.get("selected_files", {}).get(ai_project_id)
        custom_gpu_configs = user_data.get('cerebrium_configs', [])
        raw_result = user_state.get('cerebrium_results', {}).get(ai_project_id)
        if isinstance(raw_result, dict):
            last_cerebrium_result = dict(raw_result)
            if 'status' not in last_cerebrium_result:
                last_cerebrium_result['status'] = 'completed'
            if not last_cerebrium_result.get('public_url') and last_cerebrium_result.get('output_key'):
                regenerated_url = get_public_s3_url(last_cerebrium_result['output_key'])
                if regenerated_url:
                    last_cerebrium_result['public_url'] = regenerated_url
            if 'saved_at' not in last_cerebrium_result:
                last_cerebrium_result['saved_at'] = datetime.utcnow().isoformat() + 'Z'
            if 'saved_at_ms' not in last_cerebrium_result:
                saved_value = last_cerebrium_result.get('saved_at')
                if saved_value:
                    try:
                        normalized = saved_value.replace('Z', '+00:00')
                        saved_dt = datetime.fromisoformat(normalized)
                        last_cerebrium_result['saved_at_ms'] = int(saved_dt.timestamp() * 1000)
                    except ValueError:
                        pass

    if space_card_type == 'netmind':
        chat_announcement = db.get('chat_announcement', {})
        return render_template(
            'space_netmind_chat.html',
            ai_project=ai_project,
            api_key=api_key,
            server_domain=effective_server_domain,
            announcement=announcement,
            chat_announcement=chat_announcement,
            force_pro_popup=True
        )
    
    if space_card_type == 'websockets':
        from .websocket_manager import ws_manager
        is_connected = ws_manager.is_space_connected(ai_project_id)
        queue_size = ws_manager.get_queue_size(ai_project_id) if is_connected else 0
        queue_list = ws_manager.get_queue_list(ai_project_id) if is_connected else []
        
        # Check if user has GitHub bound
        user = db.get('users', {}).get(username) if username else None
        is_github_user = user.get('is_github_user', False) if user else False
        
        return render_template(
            'space_websockets.html',
            ai_project=ai_project,
            is_connected=is_connected,
            queue_size=queue_size,
            queue_list=queue_list,
            announcement=announcement,
            current_username=username,
            server_domain=effective_server_domain,
            is_github_user=is_github_user,
            force_pro_popup=True
        )

    if space_card_type == 'modelscope':
        from .modelscope_config import get_model_by_id
        from .modelscope_handler import modelscope_manager
        
        ms_config = ai_project.get('modelscope_config', {})
        timeout_seconds = ms_config.get('timeout_seconds', 300)
        
        # Get configured model object
        model_id = ms_config.get('model_id', 'Tongyi-MAI/Z-Image-Turbo')
        current_model = get_model_by_id(model_id)

        # Get current inference status if user is logged in
        inference_status = None
        can_submit = True
        wait_seconds = 0
        
        if username:
            can_submit, reason, wait_seconds = modelscope_manager.can_user_start_inference(
                username, ai_project_id, timeout_seconds
            )
            inference_status = modelscope_manager.check_inference_status(username, ai_project_id)
        
        # Check if user has GitHub bound (for showing prompt)
        user = db.get('users', {}).get(username) if username else None
        is_github_user = user.get('is_github_user', False) if user else False
        
        return render_template(
            'space_modelscope.html',
            ai_project=ai_project,
            current_model=current_model,
            timeout_seconds=timeout_seconds,
            enabled_resolutions=ms_config.get('enabled_resolutions', ['1024x1024']),
            announcement=announcement,
            can_submit=can_submit,
            wait_seconds=wait_seconds,
            inference_status=inference_status,
            is_admin=session.get('is_admin', False),
            is_github_user=is_github_user,
            force_pro_popup=True
        )

    s3_public_base_url = None
    s3_config = get_s3_config()
    if s3_config:
        endpoint = s3_config.get('S3_ENDPOINT_URL')
        bucket = s3_config.get('S3_BUCKET_NAME')
        if endpoint and bucket:
            s3_public_base_url = f"{endpoint.rstrip('/')}/{bucket}"

    param_form_html = ""
    if ai_project.get("params") and isinstance(ai_project["params"], list):
        for param in ai_project["params"]:
            name = param.get("name", "")
            label = param.get("label", name)
            param_type = param.get("type", "text")
            default = param.get("default", "")
            help_text = param.get("help_text", "")

            input_html = ''
            if param_type == 'boolean':
                checked = 'checked' if default else ''
                input_html = f'<input type="checkbox" name="{name}" id="param_{name}" {checked} style="width: auto;">'
            else:
                input_html = f'<input type="{param_type}" name="{name}" id="param_{name}" value="{default}" class="form-control" style="width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box;">'

            help_html = f'<small style="color: #666; display: block; margin-top: 4px;">{help_text}</small>' if help_text else ''

            param_form_html += f'''
                <div class="form-group">
                    <label for="param_{name}">{label}</label>
                    {input_html}
                    {help_html}
                </div>
            '''

    # --- Hreflang and API URL generation ---
    server_domain = effective_server_domain or raw_server_domain or current_request_domain
    def external_url_for(endpoint, **values):
        if '127.0.0.1' in server_domain or 'localhost' in server_domain:
             return url_for(endpoint, **values, _external=True)
        return f"{server_domain}{url_for(endpoint, **values)}"

    hreflang_links = [
        {'lang': 'zh', 'url': external_url_for('main.ai_project_view', ai_project_id=ai_project_id)},
        {'lang': 'en', 'url': external_url_for('main.ai_project_view', ai_project_id=ai_project_id, lang='en')},
        {'lang': 'x-default', 'url': external_url_for('main.ai_project_view', ai_project_id=ai_project_id)}
    ]

    # --- API Examples Generation ---
    api_examples = []
    api_base_url = f"{server_domain}/api/v1" if server_domain else "/api/v1"
    run_api_url = f"{api_base_url}/spaces/run"
    status_api_url_base = f"{api_base_url}/task"

    for template_id, template_data in ai_project.get('templates', {}).items():
        template_name = template_data.get('name', f'Template {template_id}')
        space_name = ai_project.get('name')

        # Base payload
        payload = {
            "space_name": space_name,
            "gpu_template": template_name,
        }
        # Add prompt or file_url based on template config
        if template_data.get('disable_prompt'):
            payload["file_url"] = "https://example.com/path/to/your/file.glb"
        else:
            payload["prompt"] = "Your creative prompt here"

        # --- cURL Example ---
        json_payload_curl = json.dumps(payload, indent=2)
        curl_command = (
            f'curl -X POST "{run_api_url}" \\\n'
            f'-H "Authorization: Bearer {api_key}" \\\n'
            f'-H "Content-Type: application/json" \\\n'
            f"-d '{json_payload_curl}'"
        )

        # --- Python Async Example ---
        payload_str_py = json.dumps(payload, indent=4)
        python_async_command = f'''
import requests
import time
import json

API_KEY = "{api_key}"
BASE_URL = "{api_base_url}"
PAYLOAD = {payload_str_py.replace('true', 'True').replace('false', 'False')}

headers = {{
    "Authorization": f"Bearer {{API_KEY}}",
    "Content-Type": "application/json"
}}

# 1. Start task
print(">>> Starting task...")
start_response = requests.post(f"{{BASE_URL}}/spaces/run", json=PAYLOAD, headers=headers)
start_response.raise_for_status()
task_id = start_response.json()["task_id"]
print(f"Task started successfully, Task ID: {{task_id}}")

# 2. Poll for status
print("\\n>>> Polling for status...")
while True:
    status_response = requests.get(f"{{BASE_URL}}/task/{{task_id}}/status", headers=headers)
    status_response.raise_for_status()
    data = status_response.json()
    status = data.get("status")
    print(f"Current task status: {{status}}")

    if status in ["completed", "failed"]:
        print("\\n--- Task Finished ---")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        break

    time.sleep(5)
'''

        # --- Python Stream Example ---
        stream_payload = payload.copy()
        stream_payload["stream"] = True
        stream_payload_str_py = json.dumps(stream_payload, indent=4)
        python_stream_command = f'''
import requests
import json

API_KEY = "{api_key}"
BASE_URL = "{api_base_url}"
PAYLOAD = {stream_payload_str_py.replace('true', 'True').replace('false', 'False')}

headers = {{
    "Authorization": f"Bearer {{API_KEY}}",
    "Content-Type": "application/json"
}}

print(">>> Starting stream...")
with requests.post(f"{{BASE_URL}}/spaces/run", json=PAYLOAD, headers=headers, stream=True) as response:
    response.raise_for_status()
    for line in response.iter_lines():
        if line:
            print(line.decode('utf-8'))
'''

        api_examples.append({
            'name': template_name,
            'id': template_id,
            'curl': curl_command.strip(),
            'python_async': python_async_command.strip(),
            'python_stream': python_stream_command.strip()
        })

    return render_template(
        "ai_project_view.html",
        ai_project=ai_project,
        param_form_html=param_form_html,
        api_key=api_key,
        is_waiting_for_file=is_waiting_for_file,
        announcement=announcement,
        user_has_invitation_code=user_has_invitation_code,
        selected_file_key=selected_file_key,
        hreflang_links=hreflang_links,
        api_examples=api_examples,
        space_card_type=space_card_type,
        s3_public_base_url=s3_public_base_url,
        current_username=username,
        custom_gpu_configs=custom_gpu_configs,
        last_custom_gpu_result=last_cerebrium_result,
        custom_gpu_timeout_seconds=cerebrium_timeout_seconds,
        force_pro_popup=True
    )

@main_bp.route("/run_inference/<ai_project_id>", methods=["POST"])
def run_inference(ai_project_id):
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401

    db = load_db()

    # Sensitive word check
    sensitive_words = db.get('sensitive_words', [])
    prompt = request.form.get('prompt', '')
    if sensitive_words:
        for word in sensitive_words:
            if word in prompt:
                return jsonify({'error': f'您的prompt中包含敏感词“{word}”，请修改后再提交。'}), 400

    username = session['username']

    if db.get('user_states', {}).get(username, {}).get('is_waiting_for_file'):
        return jsonify({'error': '您已经有一个任务正在运行，请等待其完成后再试。'}), 429

    ai_project = db["spaces"].get(ai_project_id)
    if not ai_project:
        return jsonify({'error': 'AI Project 未找到'}), 404

    template_id = request.form.get('template_id')
    if not template_id:
        return jsonify({'error': '请选择一个模板'}), 400

    template = ai_project.get('templates', {}).get(template_id)
    if not template:
        return jsonify({'error': '选择的模板无效'}), 404

    # Backend enforcement for restricted templates
    user_data = db['users'].get(username, {})
    if template.get('requires_invitation_code') and not user_data.get('has_invitation_code'):
        return jsonify({'error': '此模板需要邀请码，请在个人资料页面绑定。'}), 403

    s3_keys_str = request.form.get('s3_object_keys')
    if template.get('force_upload') and not s3_keys_str and not request.files.getlist('input_file'):
        return jsonify({'error': '此模板需要上传文件'}), 400

    user_data = db['users'].get(username)
    user_api_key = user_data.get('api_key')
    if not user_api_key:
        return jsonify({'error': '用户API密钥缺失'}), 500

    server_url = db.get('settings', {}).get('server_domain')
    if not server_url:
        return jsonify({'error': '服务器域名未配置'}), 500

    prompt = request.form.get('prompt', '')
    base_cmd = template.get("base_command", "")
    full_cmd = base_cmd
    if not template.get('disable_prompt'):
        full_cmd += f' --prompt {shlex.quote(prompt)}'

    preset_params = template.get("preset_params", "").strip()
    if preset_params:
        full_cmd += f' {preset_params}'

    # Handle file selected from S3 browser
    if s3_keys_str:
        s3_keys = json.loads(s3_keys_str)
        if s3_keys:
            s3_key = s3_keys[0]  # Use the first selected file
            file_url = get_public_s3_url(s3_key)

            if file_url:
                s3_key_lower = s3_key.lower()
                if s3_key_lower.endswith('.safetensors'):
                    arg_name = '--lora'
                elif s3_key_lower.endswith('.glb'):
                    arg_name = '--input_model'
                else:
                    arg_name = '--input_image'
                full_cmd += f' {arg_name} {shlex.quote(file_url)}'
            else:
                return jsonify({'error': '无法为所选文件生成下载链接，请检查S3配置。'}), 500

    temp_upload_paths = []
    files = request.files.getlist('input_file')

    if template.get('enable_lora_upload') and len(files) > 0:
        if len(files) > 2:
            return jsonify({'error': 'Lora模式最多只能上传2个文件。'}), 400

        safetensors_file = None
        other_file = None
        for file in files:
            if file.filename.endswith('.safetensors'):
                safetensors_file = file
            else:
                other_file = file

        if safetensors_file and other_file:
            # Handle safetensors file
            sf_filename = secure_filename(safetensors_file.filename)
            sf_unique_filename = f"{uuid.uuid4()}_{sf_filename}"
            upload_dir = os.path.join(current_app.instance_path, current_app.config['UPLOAD_FOLDER'])
            os.makedirs(upload_dir, exist_ok=True)
            sf_temp_path = os.path.join(upload_dir, sf_unique_filename)
            safetensors_file.save(sf_temp_path)
            temp_upload_paths.append(sf_temp_path)
            lora_url = f"{server_url}/uploads/{sf_unique_filename}"
            full_cmd += f' --lora {shlex.quote(lora_url)}'

            # Handle other file
            ot_filename = secure_filename(other_file.filename)
            ot_unique_filename = f"{uuid.uuid4()}_{ot_filename}"
            ot_temp_path = os.path.join(upload_dir, ot_unique_filename)
            other_file.save(ot_temp_path)
            temp_upload_paths.append(ot_temp_path)
            file_url = f"{server_url}/uploads/{ot_unique_filename}"

            ext = ot_filename.rsplit('.', 1)[1].lower()
            if ext == 'glb':
                arg_name = '--input_model'
            else:
                arg_map = {
                    'png': '--input_image', 'jpg': '--input_image', 'jpeg': '--input_image', 'gif': '--input_image',
                    'wav': '--input_audio', 'mp3': '--input_audio', 'ogg': '--input_audio',
                    'mp4': '--input_video', 'webm': '--input_video', 'mov': '--input_video'
                }
                arg_name = arg_map.get(ext, '--input_image')
            full_cmd += f' {arg_name} {shlex.quote(file_url)}'

        elif len(files) == 1: # Only one file was uploaded
            file = files[0]
            if file and file.filename != '':
                filename = secure_filename(file.filename)
                unique_filename = f"{uuid.uuid4()}_{filename}"
                upload_dir = os.path.join(current_app.instance_path, current_app.config['UPLOAD_FOLDER'])
                os.makedirs(upload_dir, exist_ok=True)
                temp_path = os.path.join(upload_dir, unique_filename)
                file.save(temp_path)
                temp_upload_paths.append(temp_path)
                file_url = f"{server_url}/uploads/{unique_filename}"

                ext = filename.rsplit('.', 1)[1].lower()
                if ext == 'safetensors':
                    full_cmd += f' --lora {shlex.quote(file_url)}'
                elif ext == 'glb':
                    full_cmd += f' --input_model {shlex.quote(file_url)}'
                else:
                    arg_map = {
                        'png': '--input_image', 'jpg': '--input_image', 'jpeg': '--input_image', 'gif': '--input_image',
                        'wav': '--input_audio', 'mp3': '--input_audio', 'ogg': '--input_audio',
                        'mp4': '--input_video', 'webm': '--input_video', 'mov': '--input_video'
                    }
                    arg_name = arg_map.get(ext, '--input_image')
                    full_cmd += f' {arg_name} {shlex.quote(file_url)}'

    elif len(files) > 0: # Not a lora-enabled template, handle first file only
        file = files[0]
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            unique_filename = f"{uuid.uuid4()}_{filename}"
            upload_dir = os.path.join(current_app.instance_path, current_app.config['UPLOAD_FOLDER'])
            os.makedirs(upload_dir, exist_ok=True)
            temp_path = os.path.join(upload_dir, unique_filename)
            file.save(temp_path)
            temp_upload_paths.append(temp_path)
            file_url = f"{server_url}/uploads/{unique_filename}"

            ext = filename.rsplit('.', 1)[1].lower()
            if ext == 'glb':
                arg_name = '--input_model'
            else:
                arg_map = {
                    'png': '--input_image', 'jpg': '--input_image', 'jpeg': '--input_image', 'gif': '--input_image',
                    'wav': '--input_audio', 'mp3': '--input_audio', 'ogg': '--input_audio',
                    'mp4': '--input_video', 'webm': '--input_video', 'mov': '--input_video'
                }
                arg_name = arg_map.get(ext, '--input_image')
            full_cmd += f' {arg_name} {shlex.quote(file_url)}'

    seed = None
    if template.get("params") and isinstance(template["params"], list):
        param_parts = []
        for param in template["params"]:
            name = param.get("name")
            if not name: continue

            user_value = request.form.get(name)
            if name.lower() == 'seed' and user_value:
                try:
                    seed = int(user_value)
                except (ValueError, TypeError):
                    seed = str(user_value)

            param_type = param.get("type", "text")
            if param_type == 'boolean':
                if user_value:
                    param_parts.append(f"--{name}")
            else:
                final_value = user_value if user_value is not None else param.get("default", "")
                if final_value:
                    param_parts.append(f"--{name}")
                    param_parts.append(shlex.quote(str(final_value)))
        if param_parts:
            full_cmd += " " + " ".join(param_parts)

    # --- S3 Upload Logic ---
    # Use the admin-defined filename if it exists, otherwise predict it.
    predicted_filename = template.get('predicted_output_filename') or predict_output_filename(prompt, seed)

    if template.get('disable_prompt') and not template.get('predicted_output_filename'):
        predicted_filename = 'output/output.glb'

    # Get the file extension from the predicted filename
    file_ext = os.path.splitext(predicted_filename)[1]
    if not file_ext:
        # Default to .png if the extension is missing
        file_ext = '.png'

    # Create the new filename format: usernameYYYYMMDDHHMM.ext
    timestamp = datetime.now().strftime('%Y%m%d%H%M')
    new_filename = f"{username}{timestamp}{file_ext}"

    # Prepend username to create a unique "folder" for the user in S3
    s3_object_name = f"{username}/{new_filename}"

    # Generate a presigned URL for the S3 object name
    s3_urls = generate_presigned_url(s3_object_name)
    if not s3_urls:
        return jsonify({'error': '无法生成上传URL，请检查S3设置是否正确。'}), 500

    presigned_url = s3_urls['presigned_url']
    final_url = s3_urls['final_url']
    # --- End S3 Upload Logic ---

    task_id = str(uuid.uuid4())
    thread = threading.Thread(target=execute_inference_task, args=(
        task_id, username, full_cmd, temp_upload_paths, user_api_key, server_url,
        template, prompt, seed, presigned_url, s3_object_name, predicted_filename
    ))
    thread.start()

    # Increment user's run and inference counts
    user_data = db['users'].get(username, {})
    user_data['run_count'] = user_data.get('run_count', 0) + 1
    user_data['inference_count'] = user_data.get('inference_count', 0) + 1

    if 'user_states' not in db:
        db['user_states'] = {}
    db['user_states'][username] = {
        'is_waiting_for_file': True,
        'task_id': task_id,
        'ai_project_id': ai_project_id,
        'template_id': template_id,
        'start_time': time.time()
    }
    save_db(db)

    return jsonify({'task_id': task_id})


@main_bp.route('/uploads/<path:filename>')
def uploaded_file(filename):
    upload_dir = os.path.join(current_app.instance_path, current_app.config['UPLOAD_FOLDER'])
    return send_from_directory(upload_dir, filename)


@main_bp.route('/chat')
def chat():
    return render_template('chat.html')

@main_bp.route('/chat/history')
def chat_history():
    return render_template('chat_history.html')

@main_bp.route('/check_status/<task_id>')
def check_status(task_id):
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401

    task = tasks.get(task_id)
    if not task:
        return jsonify({'status': 'not_found'}), 404

    # Make a copy to modify
    task_copy = task.copy()

    # Mask logs for non-admin users
    if not session.get('is_admin'):
        task_copy['logs'] = '****'

    return jsonify(task_copy)


@main_bp.route('/set_avatar', methods=['POST'])
def set_avatar():
    if not session.get('logged_in'):
        return jsonify({'success': False, 'error': 'Authentication required'}), 401

    data = request.get_json()
    s3_key = data.get('s3_key')

    if not s3_key:
        return jsonify({'success': False, 'error': 'Missing s3_key'}), 400

    username = session['username']
    # Security check: ensure the user is selecting a file from their own directory
    if not s3_key.startswith(f"{username}/"):
        return jsonify({'success': False, 'error': 'Authorization denied'}), 403

    db = load_db()
    s3_config = get_s3_config()
    if not all([s3_config.get('S3_ENDPOINT_URL'), s3_config.get('S3_BUCKET_NAME')]):
         return jsonify({'success': False, 'error': 'S3 is not configured on the server.'}), 500

    # Construct the public URL
    avatar_url = f"{s3_config['S3_ENDPOINT_URL']}/{s3_config['S3_BUCKET_NAME']}/{s3_key}"

    db['users'][username]['avatar'] = avatar_url
    save_db(db)

    # Update the session so the change is reflected immediately in the header
    session['user_avatar'] = avatar_url

    return jsonify({'success': True, 'new_avatar_url': avatar_url})

@main_bp.route('/api/check_inference_status')
def check_inference_status():
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401

    db = load_db()
    username = session['username']
    user_state = db.get('user_states', {}).get(username)

    if not user_state or not user_state.get('is_waiting_for_file'):
        return jsonify({'is_waiting': False})

    ai_project_id = user_state.get("ai_project_id")
    template_id = user_state.get("template_id")
    ai_project = db.get("spaces", {}).get(ai_project_id)

    if not ai_project or not template_id:
        user_state["is_waiting_for_file"] = False
        save_db(db)
        return jsonify({"is_waiting": False, "error": "Project or template not found for running task."})

    template = ai_project.get('templates', {}).get(template_id)
    if not template:
        user_state["is_waiting_for_file"] = False
        save_db(db)
        return jsonify({"is_waiting": False, "error": "Template configuration is missing."})

    timeout = template.get("timeout", 300)
    start_time = user_state.get('start_time', 0)

    if time.time() - start_time > timeout:
        user_state['is_waiting_for_file'] = False
        save_db(db)
        # Per user request, do not send a specific timeout message.
        # The button will just become re-enabled on the frontend.
        return jsonify({'is_waiting': False})

    return jsonify({'is_waiting': True})

# --- Public Article Routes ---

@main_bp.route('/articles')
def article_list():
    db = load_db()
    # Sort articles by creation date, newest first
    articles = sorted(db.get('articles', {}).values(), key=lambda x: x.get('created_at', ''), reverse=True)
    return render_template('articles.html', articles=articles)

@main_bp.route('/privacy')
def privacy_policy():
    """Renders the privacy policy page."""
    return render_template('privacy.html')


@main_bp.route('/article/<slug>')
def article_view(slug):
    db = load_db()
    # Find article by slug
    article = next((a for a in db.get('articles', {}).values() if a['slug'] == slug), None)

    if not article:
        return "Article not found", 404

    # --- Hreflang links generation ---
    server_domain = db.get('settings', {}).get('server_domain', request.url_root.rstrip('/'))
    def external_url_for(endpoint, **values):
        if '127.0.0.1' in server_domain or 'localhost' in server_domain:
             return url_for(endpoint, **values, _external=True)
        return f"{server_domain}{url_for(endpoint, **values)}"

    hreflang_links = [
        {'lang': 'zh', 'url': external_url_for('main.article_view', slug=slug)},
        {'lang': 'en', 'url': external_url_for('main.article_view', slug=slug, lang='en')},
        {'lang': 'x-default', 'url': external_url_for('main.article_view', slug=slug)}
    ]

    return render_template('article_view.html', article=article, hreflang_links=hreflang_links)

# AdSense Required Pages
@main_bp.route('/about')
def about():
    """关于我们页面"""
    db = load_db()
    settings = db.get('settings', {})
    return render_template('about.html', settings=settings)

@main_bp.route('/privacy')
def privacy():
    """隐私政策页面"""
    db = load_db()
    settings = db.get('settings', {})
    return render_template('privacy.html', settings=settings)

@main_bp.route('/terms')
def terms():
    """使用条款页面"""
    db = load_db()
    settings = db.get('settings', {})
    return render_template('terms.html', settings=settings)

@main_bp.route('/contact')
def contact():
    """联系我们页面"""
    db = load_db()
    settings = db.get('settings', {})
    return render_template('contact.html', settings=settings)

@main_bp.route('/faq')
def faq():
    """常见问题页面"""
    db = load_db()
    settings = db.get('settings', {})
    return render_template('faq.html', settings=settings)

@main_bp.route('/websockets/submit/<ai_project_id>', methods=['POST'])
def submit_websockets_request(ai_project_id):
    """Submit an inference request to a WebSocket-connected space"""
    if not session.get('logged_in'):
        return jsonify({'error': '未登录'}), 401

    db = load_db()
    ai_project = db["spaces"].get(ai_project_id)
    if not ai_project or ai_project.get('card_type') != 'websockets':
        return jsonify({'error': 'Space 未找到或类型不正确'}), 404

    from .websocket_manager import ws_manager
    from .websocket_handler import send_inference_request
    
    if not ws_manager.is_space_connected(ai_project_id):
        return jsonify({'error': '远程应用未连接'}), 503

    username = session.get('username')
    request_id = str(uuid.uuid4())

    from .usage_limiter import check_and_increment_usage

    # 1. Check Rate Limit (Cooldown) FIRST to avoid incrementing usage on rejected requests
    websockets_config = ai_project.get('websockets_config', {})
    rate_limit_seconds = websockets_config.get('rate_limit_seconds', 0)

    if rate_limit_seconds > 0:
        if 'user_states' not in db:
            db['user_states'] = {}
        if username not in db['user_states']:
            db['user_states'][username] = {}

        user_state = db['user_states'][username]
        if 'last_ws_request_times' not in user_state:
            user_state['last_ws_request_times'] = {}

        last_request_time = user_state['last_ws_request_times'].get(ai_project_id, 0)
        now = time.time()

        if now - last_request_time < rate_limit_seconds:
            remaining = int(rate_limit_seconds - (now - last_request_time)) + 1
            return jsonify({'error': f'请求过于频繁，请等待 {remaining} 秒后再试。', 'retry_after': remaining}), 429

        # Update last request time (will be saved below)
        user_state['last_ws_request_times'][ai_project_id] = now

    # 2. Check Daily Usage Limit
    allowed, error_msg = check_and_increment_usage(db, username, 'websocket')
    if not allowed:
        return jsonify({'error': error_msg}), 403

    # 3. Save all DB changes (usage increment + rate limit timestamp)
    save_db(db)

    payload = {
        'prompt': request.form.get('prompt', ''),
        'timestamp': datetime.utcnow().isoformat()
    }

    # Handle audio URL from form input
    audio_url = request.form.get('audio_url', '').strip()
    if audio_url:
        payload['audio'] = audio_url
    else:
        return jsonify({'error': '缺少音频直链'}), 400

    # Handle video file upload (TODO)
    if request.files.get('video_file'):
        pass

    # Handle other file upload (TODO)
    if request.files.get('other_file'):
        pass

    success, result = ws_manager.queue_inference_request(ai_project_id, request_id, username, payload)
    if not success:
        return jsonify({'error': result}), 500

    upload = None
    try:
        from .s3_utils import get_s3_client, get_s3_config, generate_presigned_url
        s3_client = get_s3_client()
        s3_config = get_s3_config()
        bucket_name = (s3_config or {}).get('S3_BUCKET_NAME')
        if s3_client and bucket_name:
            object_key = f"{username}/ws_results/{request_id[:8]}_{uuid.uuid4().hex[:8]}.wav"
            presigned = generate_presigned_url(object_key, content_type='audio/wav', expiration=3600)
            if presigned:
                upload = {
                    'put_url': presigned.get('presigned_url'),
                    'final_url': presigned.get('final_url'),
                    'content_type': 'audio/wav'
                }
    except Exception:
        upload = None

    current_app.logger.info('[websockets] presign_generated=%s request_id=%s', bool(upload), request_id)
    print('[websockets] presign_generated=%s request_id=%s' % (bool(upload), request_id))

    # Send the request to the connected remote app
    request_data = {
        'request_id': request_id,
        'username': username,
        'payload': payload
    }
    if upload and upload.get('put_url') and upload.get('final_url'):
        request_data['upload'] = upload
    send_inference_request(ai_project_id, request_data)

    return jsonify({'success': True, 'request_id': request_id})

@main_bp.route('/websockets/status', methods=['GET'])
def get_websockets_request_status():
    """Get the status of a WebSocket inference request"""
    request_id = request.args.get('request_id')
    if not request_id:
        return jsonify({'error': 'Missing request_id'}), 400

    from .websocket_manager import ws_manager
    request_data = ws_manager.get_request_status(request_id)
    if not request_data:
        return jsonify({'error': 'Request not found'}), 404

    return jsonify({
        'request_id': request_id,
        'status': request_data.get('status'),
        'result': request_data.get('result'),
        'created_at': request_data.get('created_at'),
        'updated_at': request_data.get('updated_at')
    })

# ========== ModelScope Image Generation Routes ==========
@main_bp.route('/api/image_gen/submit/<ai_project_id>', methods=['POST'])
def modelscope_submit(ai_project_id):
    """Submit a ModelScope image generation request."""
    if not session.get('logged_in'):
        return jsonify({'success': False, 'error': '请先登录'}), 401
    
    username = session['username']
    db = load_db()
    
    # Check GitHub binding requirement
    user = db.get('users', {}).get(username)
    if not user:
        return jsonify({'success': False, 'error': '用户不存在'}), 401
    
    pro_settings = db.get('pro_settings', {})
    if pro_settings.get('force_github_binding') and not user.get('is_github_user'):
        return jsonify({
            'success': False, 
            'error': '需要绑定 GitHub 才能使用此功能',
            'require_github': True
        }), 403
    
    space = db['spaces'].get(ai_project_id)
    if not space:
        return jsonify({'success': False, 'error': 'Space not found'}), 404
    
    if space.get('card_type') != 'modelscope':
        return jsonify({'success': False, 'error': 'Invalid space type'}), 400
    
    # Get ModelScope settings
    ms_settings = db.get('modelscope_settings', {})
    keys = ms_settings.get('keys', [])
    if not keys:
        return jsonify({'success': False, 'error': '管理员未配置 ModelScope Token'}), 500
    
    # Get space config
    ms_config = space.get('modelscope_config', {})
    timeout_seconds = ms_config.get('timeout_seconds', 300)
    
    # Check if user can start inference
    from .modelscope_handler import modelscope_manager
    can_start, reason, wait_seconds = modelscope_manager.can_user_start_inference(
        username, ai_project_id, timeout_seconds
    )
    
    if not can_start:
        return jsonify({
            'success': False,
            'error': f'请等待当前任务完成',
            'reason': reason,
            'wait_seconds': wait_seconds
        }), 429

    # Check Usage Limit
    from .usage_limiter import check_and_increment_usage
    allowed, error_msg = check_and_increment_usage(db, username, 'modelscope')
    if not allowed:
        return jsonify({'success': False, 'error': error_msg}), 403
    
    # Get request data
    data = request.get_json() or {}
    prompt = data.get('prompt', '').strip()
    model_id = data.get('model_id') or ms_config.get('model_id', 'Tongyi-MAI/Z-Image-Turbo')
    resolution_id = data.get('resolution_id', '1024x1024')
    image_urls = data.get('image_urls', [])
    
    if not prompt:
        return jsonify({'success': False, 'error': '请输入提示词'}), 400
    
    # Validate resolution
    enabled_resolutions = ms_config.get('enabled_resolutions', ['1024x1024'])
    if resolution_id not in enabled_resolutions:
        resolution_id = enabled_resolutions[0] if enabled_resolutions else '1024x1024'
    
    # Get resolution dimensions
    from .modelscope_config import get_resolution_by_id
    resolution = get_resolution_by_id(resolution_id)
    
    # Start inference
    success, result = modelscope_manager.start_inference(
        username, ai_project_id, model_id, prompt, keys, resolution, image_urls
    )
    
    if not success:
        # Log error to terminal, don't expose to frontend
        import logging
        logging.getLogger('modelscope').error(f"[ModelScope] Start failed for {username}: {result}")
        return jsonify({'success': False, 'error': '服务暂时不可用'}), 500
    
    return jsonify({
        'success': True,
        'task_id': result,
        'message': '任务已提交，正在生成...'
    })

@main_bp.route('/api/image_gen/status/<ai_project_id>', methods=['GET'])
def modelscope_status(ai_project_id):
    """Get the status of a ModelScope inference for current user."""
    if not session.get('logged_in'):
        return jsonify({'success': False, 'error': '请先登录'}), 401
    
    username = session['username']
    db = load_db()
    
    space = db['spaces'].get(ai_project_id)
    if not space:
        return jsonify({'success': False, 'error': 'Space not found'}), 404
    
    ms_config = space.get('modelscope_config', {})
    timeout_seconds = ms_config.get('timeout_seconds', 300)
    
    from .modelscope_handler import modelscope_manager
    
    # Check if user can start new inference
    can_start, reason, wait_seconds = modelscope_manager.can_user_start_inference(
        username, ai_project_id, timeout_seconds
    )
    
    # Get current inference status
    inference = modelscope_manager.check_inference_status(username, ai_project_id)
    
    if not inference:
        return jsonify({
            'success': True,
            'can_submit': True,
            'status': 'idle',
            'wait_seconds': 0
        })
    
    # Poll for updates if still processing
    if inference.get('status') == 'processing':
        inference = modelscope_manager.poll_task_status(username, ai_project_id)
    
    response_data = {
        'success': True,
        'can_submit': can_start,
        'status': inference.get('status', 'unknown'),
        'wait_seconds': wait_seconds,
        'result_url': inference.get('result_url')
    }
    
    # Show elapsed_seconds to admin only
    is_admin = session.get('is_admin', False)
    if is_admin and inference.get('elapsed_seconds') is not None:
        response_data['elapsed_seconds'] = inference.get('elapsed_seconds')
    
    # Never expose error details to frontend - only show simple status
    # All error details are logged to terminal only
    
    return jsonify(response_data)

@main_bp.route('/api/image_gen/clear/<ai_project_id>', methods=['POST'])
def modelscope_clear(ai_project_id):
    """Clear completed inference tracking for user."""
    if not session.get('logged_in'):
        return jsonify({'success': False, 'error': '请先登录'}), 401
    
    username = session['username']
    from .modelscope_handler import modelscope_manager
    modelscope_manager.clear_inference(username, ai_project_id)
    
    return jsonify({'success': True})

@main_bp.route('/api/image_gen/models', methods=['GET'])
def modelscope_models():
    """Get list of available ModelScope models."""
    from .modelscope_config import MODELSCOPE_MODELS
    return jsonify({
        'success': True,
        'models': MODELSCOPE_MODELS
    })

