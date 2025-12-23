from datetime import datetime
from zoneinfo import ZoneInfo

def get_beijing_date_str():
    beijing_tz = ZoneInfo("Asia/Shanghai")
    return datetime.now(beijing_tz).strftime('%Y-%m-%d')

def check_and_increment_usage(db, username, usage_type):
    """
    Checks if a user can perform an action based on their role and daily limits.
    Increments the usage count if allowed.

    IMPORTANT: This function modifies the `db` object in place but does NOT save it.
    The caller is responsible for calling `save_db(db)`.

    Args:
        db (dict): The database object.
        username (str): The username.
        usage_type (str): 'chat' or 'websocket'.

    Returns:
        tuple: (allowed (bool), error_message (str or None))
    """
    user = db.get('users', {}).get(username)
    if not user:
        return False, "User not found"

    # Get limits settings
    # Default Limits
    default_limits = {
        'standard': {'daily_chat_limit': 10, 'daily_websocket_limit': 5, 'daily_modelscope_limit': 5},
        'pro': {'daily_chat_limit': 100, 'daily_websocket_limit': 50, 'daily_modelscope_limit': 50}
    }

    # Load from DB pro_settings, fallback to defaults
    pro_settings = db.get('pro_settings', {})
    usage_limits = pro_settings.get('usage_limits', default_limits)

    # Determine user role
    role = 'pro' if user.get('is_pro') else 'standard'
    role_limits = usage_limits.get(role, default_limits.get(role))

    limit_key = f'daily_{usage_type}_limit'
    limit = role_limits.get(limit_key, 0)

    # Check date and reset if needed
    today = get_beijing_date_str()
    daily_usage = user.get('daily_usage', {})

    if daily_usage.get('date') != today:
        daily_usage = {
            'date': today,
            'chat_count': 0,
            'websocket_count': 0
        }

    # Check current usage
    count_key = f'{usage_type}_count'
    current_count = daily_usage.get(count_key, 0)

    if current_count >= limit:
        return False, f"Usage limit reached for today. (Limit: {limit})"

    # Increment
    daily_usage[count_key] = current_count + 1
    user['daily_usage'] = daily_usage

    # We update the user object in the DB structure
    db['users'][username] = user

    return True, None

def decrement_usage(username, usage_type):
    """
    Decrements the usage count for a user.
    Used for refunds when an API call fails or returns an error.
    This function handles loading and saving the DB internally.
    """
    from .database import load_db, save_db

    db = load_db()
    user = db.get('users', {}).get(username)
    if not user:
        return

    # Get usage stats
    daily_usage = user.get('daily_usage', {})

    # Check date matches today (if not, it was reset anyway, so nothing to decrement from *today's* count)
    # However, if we just incremented it, the date should match.
    # If the date doesn't match, it means the day rolled over since the request started (unlikely but possible).
    # In that case, we shouldn't decrement yesterday's usage on today's counter which is 0.

    today = get_beijing_date_str()
    if daily_usage.get('date') != today:
        # Day rolled over, or usage wasn't initialized.
        return

    count_key = f'{usage_type}_count'
    current_count = daily_usage.get(count_key, 0)

    if current_count > 0:
        daily_usage[count_key] = current_count - 1
        user['daily_usage'] = daily_usage
        db['users'][username] = user
        save_db(db)
