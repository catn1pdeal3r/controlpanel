"""No production config, database credentials or HTTP calls are loaded by these tests."""
import ast
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bandwidth_policy import creation_limits, product_bandwidth
from productsexample import products


def load_worker():
    product_module = types.ModuleType('products')
    product_module.products = products
    config = types.ModuleType('config')
    config.DATABASE, config.PTERODACTYL_URL = 'mock', 'https://panel.invalid/'
    database_module = types.ModuleType('managers.database_manager')
    database_module.DatabaseManager = MagicMock()
    utils = types.ModuleType('managers.utils')
    utils.HEADERS = {'Authorization': 'Bearer mock-key'}
    credit = types.ModuleType('managers.credit_manager')
    source = ast.parse((ROOT / 'managers' / 'credit_manager.py').read_text(encoding='utf-8'))
    function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'convert_to_product')
    namespace = {'products': products}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<existing plan detection>', 'exec'), namespace)
    credit.convert_to_product = namespace['convert_to_product']
    modules = {'products': product_module, 'config': config, 'managers': types.ModuleType('managers'),
        'managers.database_manager': database_module, 'managers.utils': utils, 'managers.credit_manager': credit}
    spec = importlib.util.spec_from_file_location('tested_bandwidth_manager', ROOT / 'managers' / 'bandwidth_manager.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


worker = load_worker()
UUID = '9f96e6e1-f59e-41f4-bd3e-c6333607eee1'


class BandwidthIntegration(unittest.TestCase):
    def state(self, **values):
        return dict(status='running', discovery_page=None, next_action_at=None, delay_seconds=15, **values)

    def job(self, status='queued'):
        return dict(server_uuid=UUID, server_id=3, product_id=1, current_product=1, present=1,
            mapping_source='detected', policy=worker.encode_policy(products[1]), status=status, attempts=0)

    def tick(self, state, job, response=None, failure=None):
        cursor, connection = MagicMock(), MagicMock()
        cursor.fetchone.side_effect = [state, job]
        @contextmanager
        def db(**kwargs):
            yield connection, cursor
        with patch.object(worker, 'database', db), patch.object(worker, 'panel_request', return_value=response, side_effect=failure) as api:
            worker.run_tick()
        return cursor, connection, api

    def test_creation_fields_and_product_immutability(self):
        product = dict(products[1], bandwidth=dict(limit_gb=4, speed_mbps=25, overage_action='throttle', overage_mbps=1))
        before = json.dumps(product, sort_keys=True)
        result = creation_limits(product)
        self.assertEqual(result['bandwidth_gb'], 4)
        self.assertEqual(result['network_speed_mbps'], 25)
        self.assertTrue(result['bandwidth_custom'])
        self.assertEqual(json.dumps(product, sort_keys=True), before)

    def test_invalid_product_blocks_policy(self):
        for bad in (-1, True, '25'):
            with self.assertRaises(ValueError):
                product_bandwidth(dict(products[1], bandwidth={'speed_mbps': bad}))

    def test_identity_then_apply_use_separate_ticks(self):
        response = {'attributes': {'uuid': UUID, 'limits': {'memory':512}}}
        cursor, connection, api = self.tick(self.state(), self.job(), response)
        api.assert_called_once_with('GET', '/servers/3')
        connection.commit.assert_called_once()
        self.assertTrue(any("status='ready'" in call.args[0] for call in cursor.execute.call_args_list))
        policy = product_bandwidth(products[1])
        result = {'data': {'bandwidth_custom': True, 'saved': policy, 'effective': {
            'limit_gb':policy['bandwidth_limit_gb'], 'speed_mbps':policy['bandwidth_speed_mbps'],
            'overage_action':policy['bandwidth_overage_action'], 'overage_mbps':policy['bandwidth_overage_mbps']}}}
        cursor, _, api = self.tick(self.state(), self.job('ready'), result)
        api.assert_called_once_with('PATCH', '/servers/3/bandwidth', policy)
        self.assertTrue(any("status='complete'" in call.args[0] for call in cursor.execute.call_args_list))

    def test_wrong_uuid_never_patches(self):
        cursor, _, api = self.tick(self.state(), self.job(), {'attributes': {'uuid': '11111111-1111-1111-1111-111111111111'}})
        self.assertEqual(api.call_count, 1)
        self.assertTrue(any(call.args[1][0]=='failed' for call in cursor.execute.call_args_list if 'SET status=%s,attempts' in call.args[0]))

    def test_paused_and_not_due_make_no_api_calls(self):
        state = self.state(); state['status']='paused'
        self.assertEqual(self.tick(state,self.job())[2].call_count,0)
        state=self.state(); state['next_action_at']=datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(seconds=60)
        self.assertEqual(self.tick(state,self.job())[2].call_count,0)

    def test_rate_limit_backs_off_without_a_burst(self):
        cursor, _, api = self.tick(self.state(), self.job('ready'), failure=worker.ApiFailure(429))
        self.assertEqual(api.call_count,1)
        updates=[call.args[1] for call in cursor.execute.call_args_list if 'SET status=%s,attempts' in call.args[0]]
        self.assertEqual(updates[0][0], 'ready')
        self.assertGreater(updates[0][2], datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(seconds=45))

    def test_two_servers_same_owner_keep_separate_detected_plans(self):
        cursor=MagicMock()
        entries=[{'attributes': {'uuid':UUID,'id':3,'user':10,'name':'small','limits':{'memory':512}}},
            {'attributes': {'uuid':'11111111-1111-1111-1111-111111111111','id':4,'user':10,'name':'large','limits':{'memory':1024}}}]
        with patch.object(worker,'panel_request',return_value={'data':entries}):
            worker.discover_page(cursor,1)
        inserts=[call.args[1] for call in cursor.execute.call_args_list if 'INSERT INTO server_product_plans' in call.args[0]]
        self.assertEqual([entry[4] for entry in inserts],[1,2])
        self.assertEqual([entry[2] for entry in inserts],[10,10])

    def test_completed_unchanged_policy_is_not_requeued(self):
        cursor=MagicMock(); cursor.fetchone.return_value={'status':'complete','policy':worker.encode_policy(products[1]),'product_id':1}
        self.assertFalse(worker.put_job(cursor,UUID,products[1]))
        self.assertEqual(cursor.execute.call_count,1)

    def test_bad_saved_policy_is_a_failure(self):
        cursor, _, api=self.tick(self.state(),self.job('ready'),{'data':{'bandwidth_custom':False}})
        self.assertEqual(api.call_count,1)
        self.assertTrue(any(call.args[1][0]=='failed' for call in cursor.execute.call_args_list if 'SET status=%s,attempts' in call.args[0]))

    def test_another_worker_holds_lock_so_no_request_is_sent(self):
        connection, initial_cursor, cursor = MagicMock(), MagicMock(), MagicMock()
        connection.cursor.return_value = cursor
        cursor.fetchone.return_value = {'acquired':0}
        with patch.object(worker.DatabaseManager,'get_connection',return_value=(connection,initial_cursor)), patch.object(worker,'panel_request') as api:
            worker.run_tick()
        api.assert_not_called()
        connection.close.assert_called_once()

    def test_lock_is_released_after_database_failure(self):
        connection, initial_cursor, cursor = MagicMock(), MagicMock(), MagicMock()
        connection.cursor.return_value = cursor
        cursor.fetchone.side_effect = [{'acquired':1},{'released':1}]
        with patch.object(worker.DatabaseManager,'get_connection',return_value=(connection,initial_cursor)):
            with self.assertRaises(ValueError):
                with worker.database():
                    raise ValueError('mock failure')
        self.assertTrue(any('RELEASE_LOCK' in call.args[0] for call in cursor.execute.call_args_list))
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_admin_queue_requires_csrf_token(self):
        from flask import Blueprint, Flask
        admin_module=types.ModuleType('Routes.admin');admin_module.admin=Blueprint('admin',__name__)
        authentication=types.ModuleType('managers.authentication');authentication.admin_required=lambda f:f
        manager=types.ModuleType('managers.bandwidth_manager')
        for name in ('assign_plan','control','queue_rollout','snapshot','start_discovery'):
            setattr(manager,name,MagicMock(return_value=2))
        spec=importlib.util.spec_from_file_location('tested_bandwidth_routes',ROOT/'Routes'/'admin'/'bandwidth.py')
        route=importlib.util.module_from_spec(spec)
        product_module=types.ModuleType('products');product_module.products=products
        with patch.dict(sys.modules,{'products':product_module,'Routes.admin':admin_module,'managers.authentication':authentication,'managers.bandwidth_manager':manager}):
            spec.loader.exec_module(route)
        app=Flask('csrf-test');app.secret_key='mock';app.register_blueprint(admin_module.admin,url_prefix='/admin')
        client=app.test_client()
        with client.session_transaction() as session:
            session['bandwidth_csrf']='expected-token'
        self.assertEqual(client.post('/admin/bandwidth',data={'action':'queue','csrf_token':'wrong'}).status_code,400)
        manager.queue_rollout.assert_not_called()
        self.assertEqual(client.post('/admin/bandwidth',data={'action':'queue','csrf_token':'expected-token'}).status_code,302)
        manager.queue_rollout.assert_called_once()


if __name__ == '__main__':
    unittest.main()
