"""Account suspension policy shared by administrator actions and user notices."""
from datetime import datetime, timezone

from .database_manager import DatabaseManager

MAX_REASON_LENGTH = 2000


def validate_reason(reason):
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('Enter a reason for suspending this account.')
    if len(reason) > MAX_REASON_LENGTH:
        raise ValueError('The suspension reason must be 2,000 characters or fewer.')
    return reason


def suspension_status(email):
    row = DatabaseManager.execute_query(
        'SELECT suspended, suspension_reason FROM users WHERE email = %s', (email,))
    return {'suspended': bool(row and row[0]), 'reason': (row[1] or '') if row else ''}


def set_suspension(user_id, suspended, reason=None, actor=None):
    """Persist an explicit state under a row lock; repeated submissions do not toggle it."""
    reason = validate_reason(reason) if suspended else None
    connection, cursor = DatabaseManager.get_connection()
    try:
        cursor.execute('SELECT suspended FROM users WHERE id = %s FOR UPDATE', (user_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError('User not found.')
        if bool(row[0]) == bool(suspended):
            connection.commit()
            return False
        cursor.execute(
            'UPDATE users SET suspended = %s, suspension_reason = %s, suspended_at = %s, suspended_by = %s WHERE id = %s',
            (int(suspended), reason, datetime.now(timezone.utc).replace(tzinfo=None) if suspended else None,
             actor if suspended else None, user_id))
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()
