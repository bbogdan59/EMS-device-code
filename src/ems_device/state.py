"""Single-process durable state. Never copy this directory to another device."""
import json
import os
import sqlite3
import uuid
from pathlib import Path


class State:
    def __init__(self, path: Path, capacity: int = 17280):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
        self.db = sqlite3.connect(path / "agent.sqlite")
        os.chmod(path / "agent.sqlite", 0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
        """)
        self.capacity = capacity
        if self.get("identity") is None:
            self.set("identity", str(uuid.uuid4()))

    def get(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value, allow_nan=False)))

    def enqueue(self, item):
        with self.db:
            if self.db.execute("SELECT count(*) FROM outbox").fetchone()[0] >= self.capacity:
                raise BufferError("outbox_full: new sample refused; queued samples preserved")
            self.db.execute("INSERT INTO outbox(payload) VALUES (?)", (json.dumps(item, allow_nan=False),))

    def pending(self, limit=50):
        return [(row[0], json.loads(row[1])) for row in self.db.execute(
            "SELECT id,payload FROM outbox ORDER BY id LIMIT ?", (limit,))]

    def acknowledge(self, ids):
        with self.db:
            self.db.executemany("DELETE FROM outbox WHERE id=?", [(i,) for i in ids])

    def close(self):
        self.db.close()
