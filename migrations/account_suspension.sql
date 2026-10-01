-- Apply once to the dashboard database before deploying the suspension changes.
-- Append columns to preserve existing SELECT * positions used elsewhere.
ALTER TABLE users
    ADD COLUMN suspension_reason TEXT NULL,
    ADD COLUMN suspended_at DATETIME NULL,
    ADD COLUMN suspended_by VARCHAR(191) NULL;
