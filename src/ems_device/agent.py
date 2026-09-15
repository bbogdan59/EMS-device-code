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

    def _record_success(self, operation):
        try:
            self.state.record_success(operation)
        except Exception as exc:
            log.warning("health_record_failed operation=%s type=%s", operation, type(exc).__name__)

    def _record_error(self, operation, exc, *, rs485=False):
        try:
            self.state.record_error(operation, type(exc).__name__, rs485=rs485)
        except Exception as health_exc:
            log.warning("health_record_failed operation=%s type=%s", operation, type(health_exc).__name__)

    def sync(self):
        try:
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
            self._record_success("config")
        except Exception as exc:
            self._record_error("config", exc)
            raise

    def sample(self):
        try:
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
            self._record_success("sample")
        except Exception as exc:
            self._record_error("sample", exc, rs485=getattr(self.reader, "kind", None) == "modbus")
            raise

    def upload(self):
        try:
            rows = self.state.pending()
            if not rows:
                return
            result = self.api.call("POST", "/telemetry/batch", {"items": [item for _, item in rows]})
            # v1 has aggregate counters, not per-item ACK. Preserve ALL on partial rejection.
            counts = [result.get(k) for k in ("accepted", "duplicates", "rejected")]
            if any(type(c) is not int or c < 0 for c in counts):
                raise ValueError("invalid_batch_receipt")
            accepted, duplicates, rejected = counts
            item_results = result.get("results")
            if item_results is None:
                # Compatibilitate cu platforma veche: fara identitatea fiecarui
                # rezultat, numai succesul integral poate sterge date in siguranta.
                if rejected or accepted + duplicates != len(rows):
                    raise ValueError("partial_batch_rejection: queue retained for operator investigation")
                self.state.acknowledge([i for i, _ in rows])
            else:
                acknowledged, permanent = self._validate_item_receipts(rows, item_results, counts)
                self.state.apply_item_receipts(acknowledged, permanent)
            self._record_success("upload")
        except Exception as exc:
            self._record_error("upload", exc)
            raise

    @staticmethod
    def _validate_item_receipts(rows, item_results, aggregate_counts):
        """Validate the complete response before mutating the durable queue."""
        if not isinstance(item_results, list) or len(item_results) != len(rows):
            raise ValueError("invalid_item_receipt_count")
        statuses = {"accepted": 0, "duplicate": 0, "rejected": 0}
        acknowledged = []
        permanent = []
        for (outbox_id, payload), receipt in zip(rows, item_results, strict=True):
            if not isinstance(receipt, dict):
                raise ValueError("invalid_item_receipt")
            if receipt.get("boot_id") != payload.get("boot_id") or receipt.get("sequence") != payload.get("sequence"):
                raise ValueError("item_receipt_identity_mismatch")
            status = receipt.get("status")
            retryable = receipt.get("retryable")
            reason = receipt.get("reason_code")
            if status not in statuses or type(retryable) is not bool:
                raise ValueError("invalid_item_receipt")
            if status in {"accepted", "duplicate"}:
                if retryable or reason is not None:
                    raise ValueError("invalid_success_item_receipt")
                acknowledged.append(outbox_id)
            elif not isinstance(reason, str) or not reason or len(reason) > 120:
                raise ValueError("invalid_rejection_reason")
            elif not retryable:
                permanent.append((outbox_id, reason))
            statuses[status] += 1

        if [statuses["accepted"], statuses["duplicate"], statuses["rejected"]] != aggregate_counts:
            raise ValueError("item_receipt_aggregate_mismatch")
        return acknowledged, permanent
