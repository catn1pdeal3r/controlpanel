"""Persistent, paced product-policy rollout. Every worker tick sends at most one API request."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
from uuid import UUID

import requests
from config import DATABASE, PTERODACTYL_URL
from managers.database_manager import DatabaseManager
from managers.utils import HEADERS
from products import products
from bandwidth_policy import product_bandwidth, verify_policy
from managers.credit_manager import convert_to_product

logger = logging.getLogger(__name__)
LOCK_NAME = 'bandwidth-' + hashlib.sha256((DATABASE + PTERODACTYL_URL).encode()).hexdigest()[:40]


class RolloutBusy(ValueError):
    pass


class ApiFailure(Exception):
    def __init__(self, status=None):
        self.status = status
        super().__init__(f'Panel API returned HTTP {status}.' if status else 'Panel API connection failed or returned invalid JSON.')


@contextmanager
def database(wait=3):
    connection, initial_cursor = DatabaseManager.get_connection()
    initial_cursor.close()
    cursor = connection.cursor(dictionary=True, buffered=True)
    acquired = False
    try:
        cursor.execute('SELECT GET_LOCK(%s, %s) AS acquired', (LOCK_NAME, wait))
        acquired = cursor.fetchone()['acquired'] == 1
        if not acquired:
            raise RolloutBusy('The rollout is processing a server. Please try again shortly.')
        yield connection, cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        if acquired:
            cursor.execute('SELECT RELEASE_LOCK(%s)', (LOCK_NAME,))
            cursor.fetchone()
        cursor.close()
        connection.close()


def ensure_schema():
    """Add only our tables; existing dashboard tables are untouched."""
    with database() as (_, cursor):
        source = Path(__file__).resolve().parents[1] / 'migrations' / 'bandwidth_rollout.sql'
        for statement in source.read_text(encoding='utf-8').split(';'):
            if statement.strip():
                cursor.execute(statement)


def get_product(product_id):
    return next((p for p in products if p['id'] == product_id and not p.get('is_addon')), None)


def encode_policy(product):
    return json.dumps(product_bandwidth(product), sort_keys=True, separators=(',', ':'))


def put_job(cursor, server_uuid, product):
    policy = encode_policy(product)
    cursor.execute('SELECT status, policy, product_id FROM bandwidth_rollout_jobs WHERE server_uuid=%s', (server_uuid,))
    existing = cursor.fetchone()
    if existing and existing['policy'] == policy and existing['product_id'] == product['id'] and existing['status'] in ('queued', 'ready', 'complete'):
        return False
    cursor.execute('''INSERT INTO bandwidth_rollout_jobs (server_uuid, product_id, policy)
        VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE product_id=VALUES(product_id), policy=VALUES(policy),
        status='queued', attempts=0, next_attempt_at=NULL, last_error=NULL''', (server_uuid, product['id'], policy))
    return True


def remember_plan(attributes, product_id, source='creation', queue=False):
    product = get_product(product_id)
    if not product:
        raise ValueError('That server product does not exist.')
    target = str(UUID(attributes['uuid']))
    with database() as (_, cursor):
        cursor.execute('''INSERT INTO server_product_plans
            (server_uuid,server_id,owner_id,server_name,product_id,mapping_source)
            VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE server_id=VALUES(server_id),
            owner_id=VALUES(owner_id),server_name=VALUES(server_name),product_id=VALUES(product_id),
            mapping_source=VALUES(mapping_source),present=1''',
            (target, attributes['id'], attributes['user'], attributes['name'][:255], product_id, source))
        if queue:
            put_job(cursor, target, product)


def start_discovery():
    with database() as (_, cursor):
        cursor.execute('SELECT discovery_page FROM bandwidth_rollout_state WHERE id=1')
        if cursor.fetchone()['discovery_page'] is not None:
            raise ValueError('Server discovery is already queued. Resume it if it is paused.')
        cursor.execute('SELECT COUNT(*) AS total FROM bandwidth_rollout_jobs WHERE status IN (\'queued\',\'ready\')')
        if cursor.fetchone()['total']:
            raise ValueError('Finish the queued policies before discovering servers again.')
        cursor.execute('UPDATE server_product_plans SET present=0')
        cursor.execute("UPDATE bandwidth_rollout_state SET discovery_page=1,status='running',last_error=NULL,next_action_at=NULL WHERE id=1")


def queue_rollout():
    with database() as (_, cursor):
        cursor.execute('SELECT discovery_page FROM bandwidth_rollout_state WHERE id=1')
        if cursor.fetchone()['discovery_page'] is not None:
            raise ValueError('Wait for server discovery to finish before queueing policies.')
        cursor.execute('SELECT server_uuid,product_id FROM server_product_plans WHERE present=1 AND product_id IS NOT NULL')
        rows = cursor.fetchall()
        # Validate every product before making any queue changes.
        for row in rows:
            product = get_product(row['product_id'])
            if not product:
                raise ValueError(f"Product {row['product_id']} is missing from products.py. Review the server mappings.")
            product_bandwidth(product)
        count = sum(put_job(cursor, row['server_uuid'], get_product(row['product_id'])) for row in rows)
        cursor.execute("UPDATE bandwidth_rollout_state SET status='running',last_error=NULL WHERE id=1")
        return count


def assign_plan(server_uuid, product_id):
    target = str(UUID(server_uuid))
    if not get_product(product_id):
        raise ValueError('Choose a valid server product.')
    with database() as (_, cursor):
        cursor.execute('SELECT discovery_page FROM bandwidth_rollout_state WHERE id=1')
        if cursor.fetchone()['discovery_page'] is not None:
            raise ValueError('Wait for discovery to finish before editing mappings.')
        cursor.execute('SELECT server_uuid FROM server_product_plans WHERE server_uuid=%s AND present=1', (target,))
        if not cursor.fetchone():
            raise ValueError('That server is not in the discovered inventory.')
        cursor.execute("UPDATE server_product_plans SET product_id=%s,mapping_source='manual' WHERE server_uuid=%s", (product_id, target))
        cursor.execute('DELETE FROM bandwidth_rollout_jobs WHERE server_uuid=%s', (target,))


def control(action, delay=None):
    # A pause can be saved while a network request holds the worker lock.
    if action in ('pause', 'resume'):
        DatabaseManager.execute_query('UPDATE bandwidth_rollout_state SET status=%s WHERE id=1',
            ('paused' if action == 'pause' else 'running',))
    elif action == 'pace':
        if type(delay) is not int or not 15 <= delay <= 300:
            raise ValueError('Choose an interval from 15 to 300 seconds.')
        DatabaseManager.execute_query('UPDATE bandwidth_rollout_state SET delay_seconds=%s,next_action_at=DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND) WHERE id=1', (delay,delay))
    else:
        raise ValueError('Unknown rollout action.')


def snapshot(page=1):
    connection, cursor = DatabaseManager.get_connection()
    cursor.close()
    cursor = connection.cursor(dictionary=True, buffered=True)
    try:
        cursor.execute('SELECT * FROM bandwidth_rollout_state WHERE id=1')
        state = cursor.fetchone()
        cursor.execute('SELECT status,COUNT(*) AS total FROM bandwidth_rollout_jobs GROUP BY status')
        counts = {row['status']: row['total'] for row in cursor.fetchall()}
        cursor.execute('SELECT COUNT(*) AS total FROM server_product_plans WHERE present=1')
        total = cursor.fetchone()['total']
        cursor.execute('SELECT COUNT(*) AS total FROM server_product_plans WHERE present=1 AND product_id IS NULL')
        unresolved = cursor.fetchone()['total']
        cursor.execute('''SELECT plan.*, job.status AS job_status, job.last_error AS job_error
            FROM server_product_plans plan LEFT JOIN bandwidth_rollout_jobs job ON job.server_uuid=plan.server_uuid
            WHERE plan.present=1 ORDER BY plan.product_id IS NULL DESC,plan.server_id LIMIT 50 OFFSET %s''', ((page-1)*50,))
        return dict(state=state, counts=counts, rows=cursor.fetchall(), total=total, unresolved=unresolved, page=page, pages=max(1,(total+49)//50))
    finally:
        cursor.close()
        connection.close()


def panel_request(method, path, payload=None):
    try:
        response = requests.request(method, PTERODACTYL_URL.rstrip('/') + '/api/application' + path,
            headers=HEADERS, json=payload, timeout=25, allow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise ApiFailure(response.status_code)
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ApiFailure() from exc


def discover_page(cursor, page):
    response = panel_request('GET', f'/servers?per_page=50&page={page}')
    entries = response.get('data')
    if not isinstance(entries, list):
        raise ValueError('The panel returned an invalid server list.')
    for entry in entries:
        server = entry['attributes']
        target = str(UUID(server['uuid']))
        detected = convert_to_product(entry)
        product_id = None if detected.get('is_addon') else detected['id']
        cursor.execute('''INSERT INTO server_product_plans (server_uuid,server_id,owner_id,server_name,product_id,mapping_source)
            VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE server_id=VALUES(server_id),owner_id=VALUES(owner_id),
            server_name=VALUES(server_name),product_id=IF(product_id IS NULL,VALUES(product_id),product_id),
            mapping_source=IF(mapping_source='unmatched',VALUES(mapping_source),mapping_source),present=1''',
            (target,server['id'],server['user'],server['name'][:255],product_id,'detected' if product_id is not None else 'unmatched'))
    total_pages = int(response.get('meta', {}).get('pagination', {}).get('total_pages', 1))
    if page < total_pages:
        cursor.execute('UPDATE bandwidth_rollout_state SET discovery_page=%s,last_error=NULL WHERE id=1', (page+1,))
    else:
        cursor.execute('UPDATE bandwidth_rollout_state SET discovery_page=NULL,last_sync_at=UTC_TIMESTAMP(),last_error=NULL WHERE id=1')


def run_tick():
    """The MySQL named lock covers the network call and serializes all app workers."""
    try:
        with database(wait=0) as (connection, cursor):
            cursor.execute('SELECT * FROM bandwidth_rollout_state WHERE id=1')
            state = cursor.fetchone()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if not state or state['status'] != 'running' or (state['next_action_at'] and state['next_action_at'] > now):
                return
            cursor.execute('UPDATE bandwidth_rollout_state SET next_action_at=%s WHERE id=1', (now+timedelta(seconds=state['delay_seconds']),))
            connection.commit()
            if state['discovery_page'] is not None:
                try:
                    discover_page(cursor, state['discovery_page'])
                except (ApiFailure, ValueError, KeyError, TypeError):
                    cursor.execute("UPDATE bandwidth_rollout_state SET status='paused',last_error=%s WHERE id=1", ('Discovery failed. Check panel connectivity/API permissions, then resume.',))
                return
            cursor.execute('''SELECT job.*, plan.server_id,plan.product_id AS current_product,plan.present,plan.mapping_source
                FROM bandwidth_rollout_jobs job LEFT JOIN server_product_plans plan ON plan.server_uuid=job.server_uuid
                WHERE job.status IN ('queued','ready') AND (job.next_attempt_at IS NULL OR job.next_attempt_at<=UTC_TIMESTAMP())
                ORDER BY job.updated_at,job.server_uuid LIMIT 1''')
            job = cursor.fetchone()
            if not job:
                return
            try:
                product = get_product(job['current_product'])
                if not job['present'] or not product or job['product_id'] != job['current_product'] or job['policy'] != encode_policy(product):
                    raise ValueError('Server plan or product settings changed. Review the mapping and queue again.')
                if job['status'] == 'queued':
                    response = panel_request('GET', f"/servers/{job['server_id']}")
                    if str(UUID(response['attributes']['uuid'])) != job['server_uuid']:
                        raise ValueError('Server UUID changed. Discover servers again before applying this policy.')
                    if job['mapping_source'] == 'detected' and convert_to_product(response)['id'] != job['product_id']:
                        raise ValueError('The detected server plan changed. Review its mapping before queueing again.')
                    cursor.execute("UPDATE bandwidth_rollout_jobs SET status='ready',attempts=0,next_attempt_at=NULL WHERE server_uuid=%s", (job['server_uuid'],))
                else:
                    policy = json.loads(job['policy'])
                    response = panel_request('PATCH', f"/servers/{job['server_id']}/bandwidth", policy)
                    verify_policy(response, policy)
                    cursor.execute("UPDATE bandwidth_rollout_jobs SET status='complete',attempts=0,next_attempt_at=NULL,last_error=NULL WHERE server_uuid=%s", (job['server_uuid'],))
                cursor.execute('UPDATE bandwidth_rollout_state SET last_error=NULL WHERE id=1')
            except (ApiFailure, ValueError, KeyError, TypeError) as exc:
                attempts = job['attempts'] + 1
                retryable = isinstance(exc, ApiFailure) and (exc.status is None or exc.status == 429 or (exc.status and exc.status >= 500))
                error = str(exc) if isinstance(exc, (ApiFailure, ValueError)) else 'The panel returned an unexpected response.'
                cursor.execute('''UPDATE bandwidth_rollout_jobs SET status=%s,attempts=%s,next_attempt_at=%s,last_error=%s WHERE server_uuid=%s''',
                    (job['status'] if retryable and attempts < 3 else 'failed', attempts,
                     now+timedelta(seconds=max(state['delay_seconds'],30*2**min(attempts,6))),error[:512],job['server_uuid']))
                if isinstance(exc, ApiFailure) and (exc.status in (401,403) or (job['status']=='ready' and exc.status==404)):
                    cursor.execute("UPDATE bandwidth_rollout_state SET status='paused',last_error=%s WHERE id=1", ('Panel rejected the bandwidth API. Check the addon and API key, then requeue failures and resume.',))
    except RolloutBusy:
        return
    except Exception:
        logger.exception('Bandwidth rollout worker failed')
