"""Compact in-memory ring buffer of WARNING+/ERROR+ log lines, drained
periodically for best-effort upload to the platform's per-device debug log
(`POST /api/v1/devices/logs`, 10-day retention there -- see docs/API.md).
Losable by design, unlike the durable SQLite outbox telemetry uses: a
process restart simply starts a fresh buffer, and a failed upload is
dropped rather than retried."""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone

MAX_CODE_LENGTH = 64
MAX_DETAIL_LENGTH = 200
MAX_BUFFERED = 50  # matches the server's per-batch cap


class CompactLogBuffer(logging.Handler):
    def __init__(self, capacity: int = MAX_BUFFERED):
        super().__init__(level=logging.WARNING)
        self._buffer: deque = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # a broken format string must never crash the agent
            return
        code, _, detail = message.partition(" ")
        entry = {
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "level": "error" if record.levelno >= logging.ERROR else "warning",
            "code": code[:MAX_CODE_LENGTH] or "log",
        }
        if detail:
            entry["detail"] = detail[:MAX_DETAIL_LENGTH]
        self._buffer.append(entry)

    def drain(self) -> list[dict]:
        """Returns and clears everything buffered since the last drain."""
        entries = list(self._buffer)
        self._buffer.clear()
        return entries
