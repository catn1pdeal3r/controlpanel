"""Admin controls for discovery and paced per-server bandwidth migration."""
import hmac
import secrets

from flask import abort, current_app, flash, redirect, render_template, request, session, url_for
from managers.authentication import admin_required
from managers.bandwidth_manager import assign_plan, control, queue_rollout, snapshot, start_discovery
from bandwidth_policy import product_bandwidth
from products import products
from Routes.admin import admin


@admin.route('/bandwidth', methods=['GET', 'POST'])
@admin_required
def bandwidth():
    if request.method == 'POST':
        supplied = request.form.get('csrf_token', '')
        expected = session.get('bandwidth_csrf', '')
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, 'This form expired. Reload the Bandwidth page and try again.')
        action = request.form.get('action')
        if action not in ('discover', 'queue', 'assign', 'pause', 'resume', 'pace'):
            abort(400, 'Unknown action.')
        try:
            if action == 'discover':
                start_discovery()
                flash('Server discovery queued. It reads one page at a time without changing bandwidth settings.')
            elif action == 'queue':
                count = queue_rollout()
                flash(f'{count} server policies queued. Unchanged completed policies were skipped.')
            elif action == 'assign':
                assign_plan(request.form.get('server_uuid', ''), int(request.form.get('product_id', '')))
                flash('Server plan mapping saved. Queue policies to apply it.')
            elif action in ('pause', 'resume'):
                control(action)
                flash('Rollout paused.' if action == 'pause' else 'Rollout resumed.')
            elif action == 'pace':
                control(action, int(request.form.get('delay_seconds', '')))
                flash('Request interval updated.')
            current_app.logger.info('Bandwidth migration action %s by administrator %s', action, session.get('email'))
        except ValueError as exc:
            flash(str(exc))
        except Exception:
            current_app.logger.exception('Bandwidth migration admin action failed')
            flash('The migration database operation failed. Check the dashboard log and database permissions.')
        return redirect(url_for('admin.bandwidth'))

    if 'bandwidth_csrf' not in session:
        session['bandwidth_csrf'] = secrets.token_urlsafe(32)
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1
    data = snapshot(page)
    options = [p for p in products if not p.get('is_addon')]
    policies = {}
    for product in options:
        try:
            policies[product['id']] = product_bandwidth(product)
        except ValueError as exc:
            flash(str(exc))
    return render_template('admin/bandwidth.html', rollout=data,
        products=options, policies=policies, csrf_token=session['bandwidth_csrf'])
