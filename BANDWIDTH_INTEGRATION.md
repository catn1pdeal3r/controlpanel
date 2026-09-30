# Bandwidth integration

Install the Outbound Bandwidth panel addon before using these features. The dashboard Application API key requires Servers Read and Write.

## Product settings

Copy `productsexample.py` to `products.py` and configure each product's `bandwidth` dictionary:

```python
"bandwidth": {
    "limit_gb": 100,
    "speed_mbps": 25,
    "overage_action": "throttle",
    "overage_mbps": 1
},
```

Monthly outbound GB and normal outbound Mbps use `0` for unlimited. At the limit choose `block` or `throttle`; the reduced Mbps must be at least 1. The example keeps all products unlimited. Restart dashboard processes after configuration changes.

New servers receive their product policy at creation. Plan upgrades queue the new policy. Neither migration nor plan upgrades reset recorded allowance usage.

## Deploy and migrate

Upload this dashboard update and restart your normal dashboard service. Production startup adds `server_product_plans`, `bandwidth_rollout_state`, and `bandwidth_rollout_jobs` to the existing dashboard database. The database account needs CREATE permission on first startup. Alternatively import only `migrations/bandwidth_rollout.sql` using an account that can create tables. Do not import the old `controlpanel_template.sql` over a production database.

Open **Admin → Bandwidth**, choose **Discover**, review each server's plan, then **Queue migration**. Initial detection reuses the same RAM-based plan detector as `/admin/server/<id>` (exact RAM match, otherwise nearest RAM). Correct any ambiguous mapping before queueing. Future creation and upgrades record the selected product against the individual server UUID.

The persistent queue sends at most one API request per interval (15 seconds by default, adjustable to 300). Identity verification and policy application occur on separate ticks. Pause/resume controls, retry backoff and a shared MySQL lock prevent concurrent rollout requests across dashboard processes. Click Refresh progress to inspect results. Completed unchanged policies are not queued again.

The existing production APScheduler runs the worker; no separate service is required. `DEBUG_FRONTEND_MODE` disables the worker and automatic table setup.

Pre-addon Pterodactyl servers default to unlimited. Disable global addon defaults if you want unmigrated servers to remain unlimited.

## Tests

```sh
python -m unittest discover -s tests -p test_bandwidth_integration.py -v
```

Tests use example products and mocked API/database calls. Live database and panel verification must be performed on your deployment.
