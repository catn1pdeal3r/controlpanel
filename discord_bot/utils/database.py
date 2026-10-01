from managers.suspension_manager import set_suspension, validate_reason
from managers.database_manager import DatabaseManager
from managers.email_manager import send_email
from flask import current_app
from html import escape
from ..utils.logger import logger
class UserDB():

    def get_user_info(email):
        try:
            user_info = DatabaseManager.execute_query("SELECT credits, role, pterodactyl_id, id, suspended FROM users WHERE email = %s",(email,) )
            if not user_info:
                return "Error: User not found"
            return {"credits": user_info[0], "role": user_info[1], "pterodactyl_id": user_info[2], "id": user_info[3], "suspended": user_info[4]}
        except Exception as e:
            logger.error(f"Error fetching user info: {str(e)}")
            return "Error fetching user info"
    
    def get_discord_user_info(discord_id):
        try:
            user_info = DatabaseManager.execute_query("SELECT credits, role, pterodactyl_id, id, suspended, email FROM users WHERE discord_id = %s",(discord_id,) )
            if not user_info:
                return "Error: User not found"
            return {"credits": user_info[0], "role": user_info[1], "pterodactyl_id": user_info[2], "id": user_info[3], "suspended": user_info[4], "email": user_info[5]}
        except Exception as e:
            logger.error(f"Error fetching user info: {str(e)}")
            return "Error fetching user info"
        

    def suspend_user(email, reason, actor=None):
        reason = validate_reason(reason)
        row = DatabaseManager.execute_query("SELECT id FROM users WHERE email = %s", (email,))
        if not row:
            raise ValueError("User not found.")
        changed = set_suspension(row[0], True, reason, actor)
        if changed:
            send_email(email, 'Account Suspended', 'Your account has been suspended.<br><br>Reason: ' + escape(reason).replace('\n', '<br>') + '<br><br>Log in to the dashboard and open a ticket to appeal.', current_app._get_current_object())
        return "User suspended"

    def unsuspend_user(email):
        row = DatabaseManager.execute_query("SELECT id FROM users WHERE email = %s", (email,))
        if not row:
            raise ValueError("User not found.")
        changed = set_suspension(row[0], False)
        if changed:
            send_email(email, 'Account Unsuspended', 'Your account has been unsuspended.', current_app._get_current_object())
        return "User unsuspended"

    def get_all_users():
        try:
            total_users = DatabaseManager.execute_query("SELECT COUNT(*) FROM users")[0]
            return total_users
        except Exception as e:
            logger.error(f"Error fetching users: {str(e)}")
            return "Error fetching users"
    def get_suspended_users():
        try:
            total_users = DatabaseManager.execute_query("SELECT COUNT(*) FROM users WHERE suspended = 1")[0]
            return total_users
        except Exception as e:
            logger.error(f"Error fetching suspended users: {str(e)}")
            return "Error fetching suspended users"
    def link_discord(email, discord_id):
        try:
            DatabaseManager.execute_query("UPDATE users SET discord_id = %s WHERE email = %s",(discord_id,email))
            return "User linked to Discord"
        except Exception as e:
            logger.error(f"Error linking user to Discord: {str(e)}")
            return "Error linking user to Discord"