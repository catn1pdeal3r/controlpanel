"""Suspension checks without production configuration or database credentials."""
import ast
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask, flash, redirect, request, session, url_for, render_template

ROOT = Path(__file__).resolve().parents[1]


def load_manager():
    database = types.ModuleType('managers.database_manager')
    database.DatabaseManager = MagicMock()
    spec = importlib.util.spec_from_file_location('managers.suspension_manager', ROOT / 'managers/suspension_manager.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'managers.database_manager': database}):
        spec.loader.exec_module(module)
    return module


class SuspensionTests(unittest.TestCase):
    def setUp(self):
        self.manager = load_manager()
        self.connection, self.cursor = MagicMock(), MagicMock()
        self.manager.DatabaseManager.get_connection.return_value = self.connection, self.cursor

    def test_reason_is_required_before_writing(self):
        for reason in (None, '', '   ', 'x' * 2001):
            with self.assertRaises(ValueError):
                self.manager.set_suspension(3, True, reason, 'admin@example.invalid')
        self.manager.DatabaseManager.get_connection.assert_not_called()

    def test_reason_and_audit_metadata_are_saved_with_status(self):
        self.cursor.fetchone.return_value = (0,)
        self.assertTrue(self.manager.set_suspension(3, True, '  Repeated abuse  ', 'admin@example.invalid'))
        values = self.cursor.execute.call_args.args[1]
        self.assertEqual(values[:2], (1, 'Repeated abuse'))
        self.assertIsNotNone(values[2])
        self.assertEqual(values[3:], ('admin@example.invalid', 3))
        self.connection.commit.assert_called_once()

    def test_repeat_submission_does_not_unsuspend(self):
        self.cursor.fetchone.return_value = (1,)
        self.assertFalse(self.manager.set_suspension(3, True, 'Repeated abuse'))
        self.assertEqual(self.cursor.execute.call_count, 1)

    def test_unsuspension_clears_reason_and_metadata(self):
        self.cursor.fetchone.return_value = (1,)
        self.manager.set_suspension(3, False)
        self.assertEqual(self.cursor.execute.call_args.args[1], (0, None, None, None, 3))

    def test_missing_user_rolls_back(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(ValueError):
            self.manager.set_suspension(3, True, 'Repeated abuse')
        self.connection.rollback.assert_called_once()
        self.cursor.close.assert_called_once()
        self.connection.close.assert_called_once()

    def test_notice_escapes_reason_and_links_to_tickets(self):
        app = Flask('suspension-notice', template_folder=str(ROOT / 'templates'))
        app.add_url_rule('/tickets/', endpoint='tickets.tickets_index', view_func=lambda: '')
        with app.test_request_context('/'):
            html = render_template('partials/suspension_notice.html', account_suspension={
                'suspended': True, 'reason': '<script>alert(1)</script>'})
            self.assertNotIn('<script>', html)
            self.assertIn('&lt;script&gt;', html)
            self.assertIn('href="/tickets/"', html)
            self.assertIn('appeal', html)
            self.assertEqual(render_template('partials/suspension_notice.html', account_suspension={'suspended': False}).strip(), '')

    def test_admin_form_requires_csrf_and_explicit_action(self):
        source = ast.parse((ROOT / 'Routes/admin/users.py').read_text(encoding='utf-8'))
        handler = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'admin_toggle_suspension')
        handler.decorator_list = []
        namespace = {'request': request, 'session': session, 'flash': flash, 'redirect': redirect, 'url_for': url_for,
                     'hmac': __import__('hmac'), 'DatabaseManager': MagicMock(), 'set_suspension': MagicMock(), 'webhook_log': MagicMock()}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), '<suspension route>', 'exec'), namespace)
        app = Flask('suspension-route'); app.secret_key = 'test-only'
        app.add_url_rule('/admin/users', endpoint='admin.users', view_func=lambda: '')
        app.add_url_rule('/admin/user/toggle_suspension/<int:user_id>', view_func=namespace['admin_toggle_suspension'], methods=['POST'])
        client = app.test_client()
        with client.session_transaction() as state:
            state['email'] = 'admin@example.invalid'; state['suspension_csrf'] = 'expected'
        self.assertEqual(client.post('/admin/user/toggle_suspension/3', data={'action': 'suspend'}).status_code, 400)
        self.assertEqual(client.post('/admin/user/toggle_suspension/3', data={'csrf_token': 'expected'}).status_code, 400)
        self.assertEqual(client.post('/admin/user/toggle_suspension/3', data={'action': 'suspend', 'csrf_token': 'é'}).status_code, 400)
        namespace['DatabaseManager'].execute_query.return_value = (3, 'user@example.invalid')
        namespace['set_suspension'].side_effect = ValueError('Enter a reason.')
        self.assertEqual(client.post('/admin/user/toggle_suspension/3', data={'action': 'suspend', 'csrf_token': 'expected'}).status_code, 302)
        namespace['webhook_log'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
