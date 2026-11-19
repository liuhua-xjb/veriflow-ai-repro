-- Dedicated sidecar database. G1/G2 schemas and payloads are untouched.
CREATE TABLE audit (run_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
 payload TEXT NOT NULL, previous_hash TEXT NOT NULL, receipt_hash TEXT NOT NULL,
 PRIMARY KEY(run_id,seq));
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TABLE idempotency (key_hash TEXT PRIMARY KEY, request_hash TEXT NOT NULL, run_id TEXT NOT NULL);
CREATE TRIGGER idempotency_no_update BEFORE UPDATE ON idempotency BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER idempotency_no_delete BEFORE DELETE ON idempotency BEGIN SELECT RAISE(ABORT,'append_only'); END;
PRAGMA user_version=1;
