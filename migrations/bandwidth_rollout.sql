CREATE TABLE IF NOT EXISTS server_product_plans (
    server_uuid CHAR(36) NOT NULL PRIMARY KEY,
    server_id INT UNSIGNED NOT NULL,
    owner_id INT UNSIGNED NOT NULL,
    server_name VARCHAR(255) NOT NULL,
    product_id INT NULL,
    mapping_source VARCHAR(24) NOT NULL,
    present TINYINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX (product_id), INDEX (server_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS bandwidth_rollout_state (
    id TINYINT NOT NULL PRIMARY KEY,
    status VARCHAR(16) NOT NULL DEFAULT 'running',
    discovery_page INT NULL,
    next_action_at DATETIME NULL,
    delay_seconds INT NOT NULL DEFAULT 15,
    last_error VARCHAR(512) NULL,
    last_sync_at DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO bandwidth_rollout_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS bandwidth_rollout_jobs (
    server_uuid CHAR(36) NOT NULL PRIMARY KEY,
    product_id INT NOT NULL,
    policy TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'queued',
    attempts INT NOT NULL DEFAULT 0,
    next_attempt_at DATETIME NULL,
    last_error VARCHAR(512) NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX (status, next_attempt_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
