"""Product bandwidth policy helpers (no network access)."""


def product_bandwidth(product):
    config = product.get('bandwidth', {})
    values = {
        'bandwidth_limit_gb': config.get('limit_gb', 0),
        'bandwidth_speed_mbps': config.get('speed_mbps', 0),
        'bandwidth_overage_action': config.get('overage_action', 'block'),
        'bandwidth_overage_mbps': config.get('overage_mbps', 1),
        'bandwidth_custom': True,
    }
    for field, minimum, maximum in [('bandwidth_limit_gb', 0, 1000000),
            ('bandwidth_speed_mbps', 0, 100000), ('bandwidth_overage_mbps', 1, 100000)]:
        value = values[field]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"Product {product['id']}: {field} must be an integer from {minimum} to {maximum}.")
    if values['bandwidth_overage_action'] not in ('block', 'throttle'):
        raise ValueError(f"Product {product['id']}: overage_action must be block or throttle.")
    return values


def creation_limits(product):
    policy = product_bandwidth(product)
    return dict(product['limits'], bandwidth_gb=policy['bandwidth_limit_gb'],
        network_speed_mbps=policy['bandwidth_speed_mbps'],
        bandwidth_overage_action=policy['bandwidth_overage_action'],
        bandwidth_overage_mbps=policy['bandwidth_overage_mbps'], bandwidth_custom=True)


def verify_policy(response, policy):
    data = response.get('data', {})
    if data.get('bandwidth_custom') is not True:
        raise ValueError('The panel did not save a custom bandwidth policy.')
    for field in ('bandwidth_limit_gb', 'bandwidth_speed_mbps', 'bandwidth_overage_action', 'bandwidth_overage_mbps'):
        if data.get('saved', {}).get(field) != policy[field]:
            raise ValueError(f'The panel did not save {field}.')
    for field, saved in [('limit_gb', 'bandwidth_limit_gb'), ('speed_mbps', 'bandwidth_speed_mbps'),
            ('overage_action', 'bandwidth_overage_action'), ('overage_mbps', 'bandwidth_overage_mbps')]:
        if data.get('effective', {}).get(field) != policy[saved]:
            raise ValueError(f'The effective {field} does not match the product policy.')
