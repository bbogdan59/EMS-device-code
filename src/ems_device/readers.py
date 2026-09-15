"""No write methods. Register addresses must come from an audited model profile."""
import json
import math
from pathlib import Path

FIELDS = {"pv_power_w", "load_power_w", "battery_power_w", "grid_power_w", "battery_soc_percent"}


class Simulator:
    kind = "simulator"
    simulated = True
    telemetry_available = True
    def read(self):
        return {"pv_power_w": 2400, "load_power_w": 1000, "battery_power_w": 800,
                "grid_power_w": -600, "battery_soc_percent": 55}

    def close(self):
        pass


class ModbusReader:
    kind = "modbus"
    simulated = False
    telemetry_available = True
    def __init__(self, config, client=None):
        profile = json.loads(Path(config["profile"]).read_text())
        if profile.get("verified") is not True or not profile.get("source") or not profile.get("model") or not profile.get("firmware"):
            raise ValueError("An audited profile with model, firmware and source is required")
        self.points = profile["points"]
        self.model = profile["model"]
        if not self.points or len(self.points) > 32:
            raise ValueError("Profile needs 1..32 points")
        names = set()
        for p in self.points:
            if p["field"] not in FIELDS or p["field"] in names:
                raise ValueError("Unknown or duplicate telemetry field")
            names.add(p["field"])
            if type(p["address"]) is not int or not 0 <= p["address"] <= 65535:
                raise ValueError("Invalid zero-based address")
            if p["function"] not in {3, 4} or p["encoding"] not in {"u16", "s16"}:
                raise ValueError("Only read functions 3/4 and u16/s16 supported in v0.1")
            if not math.isfinite(p["scale"]) or p["scale"] == 0:
                raise ValueError("Invalid scale")
        self.device_id = config["device_id"]
        if type(self.device_id) is not int or not 1 <= self.device_id <= 247:
            raise ValueError("Modbus device_id must be 1..247; no broadcast")
        if client is None:
            from pymodbus.client import ModbusSerialClient
            client = ModbusSerialClient(port=config["port"], baudrate=config["baudrate"],
                                        parity=config["parity"], stopbits=config["stopbits"],
                                        bytesize=8, timeout=1, retries=1)
        self.client = client

    def read(self):
        if not self.client.connect():
            raise OSError("serial_unavailable")
        values = {}
        for p in self.points:
            fn = self.client.read_holding_registers if p["function"] == 3 else self.client.read_input_registers
            response = fn(p["address"], count=1, device_id=self.device_id)
            if response.isError() or len(response.registers) != 1:
                raise OSError("modbus_read_failed")
            raw = response.registers[0]
            if not 0 <= raw <= 65535:
                raise ValueError("invalid_register")
            if p["encoding"] == "s16" and raw >= 32768:
                raw -= 65536
            values[p["field"]] = raw * p["scale"]
        return values

    def close(self):
        self.client.close()


class DisabledReader:
    """Enrollment/heartbeat-only mode used before an audited profile exists."""

    simulated = False
    telemetry_available = False
    kind = "disabled"

    def read(self):
        raise RuntimeError("telemetry_reader_disabled")

    def close(self):
        pass
