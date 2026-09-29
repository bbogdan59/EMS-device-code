"""Offline release checks that never claim a device or open the serial port."""

import json
from pathlib import Path
import sqlite3
import tomllib

from .api import API


def check(config: Path) -> None:
    settings = tomllib.loads(config.read_text())
    api = API(settings["platform_url"])
    api.close()
    if settings.get("reader") not in {"disabled", "simulator", "modbus"}:
        raise ValueError("invalid reader")
    interval = settings.get("sample_seconds", 10)
    if type(interval) not in (int, float) or not 5 <= interval <= 3600:
        raise ValueError("invalid sample interval")
    database = Path(settings["state_dir"]) / "agent.sqlite"
    if not database.exists():
        return  # First install: provisioning creates state later, as ems-device.
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("state integrity check failed")
        for table, columns in (("settings", "key,value"), ("outbox", "id,payload"),
                               ("dead_letter", "id,original_outbox_id,payload,reason_code,failed_at")):
            connection.execute(f"SELECT {columns} FROM {table} LIMIT 0")
        for (value,) in connection.execute("SELECT value FROM settings"):
            json.loads(value)
    finally:
        connection.close()
