import logging
import math
import uuid
from datetime import datetime, timezone
from . import __version__
from .readers import FIELDS

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, state, api, reader):
        self.state, self.api, self.reader = state, api, reader
        self.boot_id = str(uuid.uuid4())  # new process, persistent queued items retain old boot IDs
        self.sequence = 0

    def sync(self):
        config = self.api.call("GET", "/config")
        if config.get("station_id") != self.api.credentials["station_id"]:
            raise ValueError("station_mismatch")
        if config.get("execution_mode") not in {"shadow", "live"}:
            raise ValueError("invalid_execution_mode")
        for key in ("config_version", "preference_version"):
            if type(config.get(key)) is not int or config[key] < 1:
                raise ValueError("invalid_config_version")
        # Cache desired station policy only: does NOT mean inverter applied it.
        self.state.set("station_config", config)
        self.api.call("POST", "/devices/heartbeat", {
            "boot_id": self.boot_id, "firmware_version": __version__,
            "capabilities": {"telemetry": self.reader.telemetry_available, "inverter_write": False,
                             "simulated": self.reader.simulated}})

    def sample(self):
        values = self.reader.read()
        if not values or not set(values) <= FIELDS:
            raise ValueError("invalid_telemetry_fields")
        for key, value in values.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError("non_finite_telemetry")
            if key in {"pv_power_w", "load_power_w"} and value < 0:
                raise ValueError("negative_unsigned_power")
            if key == "battery_soc_percent" and not 0 <= value <= 100:
                raise ValueError("soc_out_of_range")
        item = {"boot_id": self.boot_id, "sequence": self.sequence, "schema_version": 1,
                "measured_at": datetime.now(timezone.utc).isoformat(), **values,
                "quality_flags": {"simulated": self.reader.simulated},
                "raw_payload": {"reader": type(self.reader).__name__}}
        self.state.enqueue(item)
        self.sequence += 1

    def upload(self):
        rows = self.state.pending()
        if not rows:
            return
        result = self.api.call("POST", "/telemetry/batch", {"items": [item for _, item in rows]})
        # v1 has aggregate counters, not per-item ACK. Preserve ALL on partial rejection.
        counts = [result.get(k) for k in ("accepted", "duplicates", "rejected")]
        if any(type(c) is not int or c < 0 for c in counts):
            raise ValueError("invalid_batch_receipt")
        accepted, duplicates, rejected = counts
        if rejected or accepted + duplicates != len(rows):
            raise ValueError("partial_batch_rejection: queue retained for operator investigation")
        self.state.acknowledge([i for i, _ in rows])
