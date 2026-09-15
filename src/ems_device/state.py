"""Single-process durable state. Never copy this directory to another device."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .provisioning import new_identity


class State:
    def __init__(self, path: Path, capacity: int = 17280):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("outbox capacity must be a positive integer")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
        self.db = sqlite3.connect(path / "agent.sqlite")
        os.chmod(path / "agent.sqlite", 0o600)
        # WAL + FULL keeps each committed outbox item recoverable after a
        # process/power interruption as far as SQLite and the storage device
        # can guarantee. This is not a substitute for real SD-card fault tests.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
        """)
        self.capacity = capacity
        self.path = path
        self._ensure_identity()
        self._secure_database_files()

    def _secure_database_files(self):
        """WAL may contain the same secrets as the main DB; protect all files."""
        for suffix in ("", "-wal", "-shm"):
            candidate = self.path / f"agent.sqlite{suffix}"
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def _ensure_identity(self):
        """Create per-device material only after the OS image is installed.

        The old ``identity`` key is migrated in place so already-installed
        agents do not silently become a different physical device.
        """
        legacy_identity = self.get("identity")
        identity = self.get("device_identity")
        if identity is None:
            identity = new_identity()
            if legacy_identity:
                identity["installation_uuid"] = legacy_identity
            self.set("activation_code", identity.pop("activation_code"))
            self.set("device_identity", identity)
        elif "activation_code" in identity:
            if self.get("activation_code") is None:
                self.set("activation_code", identity["activation_code"])
            identity.pop("activation_code")
            self.set("device_identity", identity)
        if legacy_identity is None:
            self.set("identity", identity["installation_uuid"])
        for key in ("installation_uuid", "serial_number", "provisioning_secret"):
            if key not in identity or not identity[key]:
                raise ValueError(f"Incomplete device identity: {key}")

    def identity(self):
        identity = dict(self.get("device_identity"))
        activation_code = self.get("activation_code")
        if activation_code is not None:
            identity["activation_code"] = activation_code
        return identity

    def get(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value, allow_nan=False)))

    def _health(self):
        health = {
            "refused_samples": 0,
            "sample_errors": 0,
            "upload_errors": 0,
            "config_errors": 0,
            "rs485_errors": 0,
            "clock_regressions": 0,
        }
        health.update(self.get("operational_health") or {})
        return health

    def record_success(self, operation, *, now=None, min_interval_seconds=60):
        """Persist coarse health timestamps without writing to SD every sample."""
        if operation not in {"sample", "upload", "config"}:
            raise ValueError("unknown health operation")
        now = now or datetime.now(timezone.utc)
        health = self._health()
        key = f"last_{operation}_at"
        previous = health.get(key)
        if previous:
            elapsed = (now - datetime.fromisoformat(previous)).total_seconds()
            if elapsed < 0:
                health["clock_regressions"] = int(health.get("clock_regressions", 0)) + 1
            elif elapsed < min_interval_seconds:
                return
        health[key] = now.isoformat()
        self.set("operational_health", health)

    def record_error(self, operation, code, *, rs485=False, now=None):
        if operation not in {"sample", "upload", "config"}:
            raise ValueError("unknown health operation")
        now = now or datetime.now(timezone.utc)
        health = self._health()
        counter = f"{operation}_errors"
        health[counter] = int(health.get(counter, 0)) + 1
        if rs485:
            health["rs485_errors"] = int(health.get("rs485_errors", 0)) + 1
        health["last_error_code"] = str(code)[:120]
        health["last_error_at"] = now.isoformat()
        self.set("operational_health", health)

    def enqueue(self, item):
        full = False
        with self.db:
            if self.db.execute("SELECT count(*) FROM outbox").fetchone()[0] >= self.capacity:
                full = True
                health = self._health()
                health["refused_samples"] = int(health.get("refused_samples", 0)) + 1
                self.db.execute(
                    "INSERT OR REPLACE INTO settings VALUES (?,?)",
                    ("operational_health", json.dumps(health, allow_nan=False)),
                )
            else:
                self.db.execute("INSERT INTO outbox(payload) VALUES (?)", (json.dumps(item, allow_nan=False),))
        if full:
            raise BufferError("outbox_full: new sample refused; queued samples preserved")

    def health_snapshot(self, *, agent_version, clock_sync):
        self._secure_database_files()
        count = self.db.execute("SELECT count(*) FROM outbox").fetchone()[0]
        oldest = self.db.execute("SELECT payload FROM outbox ORDER BY id LIMIT 1").fetchone()
        newest = self.db.execute("SELECT payload FROM outbox ORDER BY id DESC LIMIT 1").fetchone()

        def measured_at(row):
            return (json.loads(row[0]).get("measured_at") if row else None)

        db_ok = self.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        files = [self.path / "agent.sqlite", self.path / "agent.sqlite-wal", self.path / "agent.sqlite-shm"]
        return {
            "agent_version": agent_version,
            "clock_sync": clock_sync,
            "database_ok": db_ok,
            "outbox": {
                "backlog": count,
                "capacity": self.capacity,
                "utilization_percent": round(count / self.capacity * 100, 2),
                "oldest_measured_at": measured_at(oldest),
                "newest_measured_at": measured_at(newest),
                "storage_bytes": sum(path.stat().st_size for path in files if path.exists()),
            },
            **self._health(),
        }

    def pending(self, limit=50):
        return [(row[0], json.loads(row[1])) for row in self.db.execute(
            "SELECT id,payload FROM outbox ORDER BY id LIMIT ?", (limit,))]

    def acknowledge(self, ids):
        with self.db:
            self.db.executemany("DELETE FROM outbox WHERE id=?", [(i,) for i in ids])

    def close(self):
        self.db.close()
