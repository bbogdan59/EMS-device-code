"""No write methods. Register addresses must come from an audited model profile."""
import json
import math
from pathlib import Path

# Cinci campuri originale v0.1 plus extensiile issue #1 (per-MPPT, per-faza,
# temperaturi, status brut, contoare cumulative). Un camp absent din profil
# ramane absent din citire -- niciodata zero inventat.
FIELDS = {
    "pv_power_w", "load_power_w", "battery_power_w", "grid_power_w", "battery_soc_percent",
    "pv1_power_w", "pv2_power_w", "pv1_voltage_v", "pv2_voltage_v", "pv1_current_a", "pv2_current_a",
    "grid_voltage_l1_v", "grid_voltage_l2_v", "grid_voltage_l3_v",
    "grid_ct_l1_w", "grid_ct_l2_w", "grid_ct_l3_w",
    "load_voltage_l1_v", "load_voltage_l2_v", "load_voltage_l3_v",
    "load_power_l1_w", "load_power_l2_w", "load_power_l3_w",
    "battery_voltage_v", "battery_current_a", "battery_temperature_c",
    "dc_temperature_c", "ac_temperature_c",
    "inverter_status_code",
    "pv_energy_total_kwh", "load_energy_total_kwh",
    "grid_import_energy_total_kwh", "grid_export_energy_total_kwh",
    "battery_charge_energy_total_kwh", "battery_discharge_energy_total_kwh",
}

# Campuri semnate legitim (baterie: +incarcare/-descarcare; putere per-faza CT
# poate reprezenta import sau export). Orice alt camp din FIELDS trebuie sa
# fie >=0 -- un rezultat negativ pentru ele indica un profil/decodare gresita,
# nu o citire valida.
SIGNED_FIELDS = {"battery_power_w", "grid_power_w", "battery_current_a", "battery_temperature_c",
                  "dc_temperature_c", "ac_temperature_c",
                  "grid_ct_l1_w", "grid_ct_l2_w", "grid_ct_l3_w"}

_ENCODINGS = {"u16", "s16", "u32", "s32"}
_WORD_ORDERS = {"low_high", "high_low"}
MAX_BLOCK_REGISTERS = 60  # sub limita Modbus (125): reduce riscul unui cadru RS485 lung pe magistrale/adaptoare zgomotoase


class Simulator:
    kind = "simulator"
    simulated = True
    telemetry_available = True
    def read(self):
        return {"pv_power_w": 2400, "load_power_w": 1000, "battery_power_w": 800,
                "grid_power_w": -600, "battery_soc_percent": 55}

    def check_ready(self):
        pass

    def close(self):
        pass


def _decode_point(point, registers):
    """Combina 1 sau 2 registre deja citite intr-o valoare cu semn si scala.

    `registers` este un dict adresa->int 0..65535 (raw, big-endian per registru,
    cum intoarce pymodbus). Ordinea cuvintelor (`word_order`) se aplica doar
    pentru 32-bit si priveste care dintre cele doua registre adiacente e cel
    "de jos"; fiecare registru individual ramane big-endian.
    """
    address = point["address"]
    encoding = point["encoding"]
    if encoding in ("u16", "s16"):
        raw = registers[address]
        if encoding == "s16" and raw >= 32768:
            raw -= 65536
    else:
        low_addr = address if point["word_order"] == "low_high" else address + 1
        high_addr = address + 1 if point["word_order"] == "low_high" else address
        raw = (registers[high_addr] << 16) | registers[low_addr]
        if encoding == "s32" and raw >= 2**31:
            raw -= 2**32
    return raw * point["scale"] + point["offset"]


class ModbusReader:
    kind = "modbus"
    simulated = False
    telemetry_available = True

    def __init__(self, config, client=None):
        profile = json.loads(Path(config["profile"]).read_text())
        if profile.get("verified") is not True or not profile.get("source") or not profile.get("model") or not profile.get("firmware"):
            raise ValueError("An audited profile with model, firmware and source is required")
        self.model = profile["model"]
        points = profile["points"]
        if not points or len(points) > 64:
            raise ValueError("Profile needs 1..64 points")

        names = set()
        normalized_points = []
        for p in points:
            if p["field"] not in FIELDS or p["field"] in names:
                raise ValueError("Unknown or duplicate telemetry field")
            names.add(p["field"])
            if type(p["address"]) is not int or not 0 <= p["address"] <= 65535:
                raise ValueError("Invalid zero-based address")
            if p["function"] not in {3, 4}:
                raise ValueError("Only read functions 3/4 supported")
            if p["encoding"] not in _ENCODINGS:
                raise ValueError("Unsupported encoding")
            if p["encoding"] in ("u32", "s32"):
                if p.get("word_order") not in _WORD_ORDERS:
                    raise ValueError("32-bit points require word_order low_high|high_low")
                if p["address"] == 65535:
                    raise ValueError("32-bit point needs a second register in range")
            scale = p["scale"]
            if not math.isfinite(scale) or scale == 0:
                raise ValueError("Invalid scale")
            offset = p.get("offset", 0)
            if not math.isfinite(offset):
                raise ValueError("Invalid offset")
            normalized_points.append({**p, "offset": offset})
        self.points = normalized_points

        computed = profile.get("computed", [])
        for c in computed:
            if c["field"] not in FIELDS or c["field"] in names:
                raise ValueError("Unknown or duplicate computed field")
            names.add(c["field"])
            if not c.get("sum_of") or any(term not in {pt["field"] for pt in self.points} for term in c["sum_of"]):
                raise ValueError("computed.sum_of must reference defined point fields")
        self.computed = computed

        blocks = profile.get("blocks")
        if blocks is not None:
            normalized_blocks = []
            covered = {}
            for b in blocks:
                if type(b["start"]) is not int or not 0 <= b["start"] <= 65535:
                    raise ValueError("Invalid block start")
                if type(b["length"]) is not int or not 1 <= b["length"] <= MAX_BLOCK_REGISTERS:
                    raise ValueError(f"Block length must be 1..{MAX_BLOCK_REGISTERS}")
                if b["start"] + b["length"] - 1 > 65535:
                    raise ValueError("Block exceeds address space")
                if b["function"] not in {3, 4}:
                    raise ValueError("Only read functions 3/4 supported")
                for addr in range(b["start"], b["start"] + b["length"]):
                    if addr in covered:
                        raise ValueError("Blocks must not overlap")
                    covered[addr] = b["function"]
                normalized_blocks.append(dict(b))
            for p in self.points:
                needed = [p["address"]] if p["encoding"] in ("u16", "s16") else [p["address"], p["address"] + 1]
                for addr in needed:
                    if covered.get(addr) != p["function"]:
                        raise ValueError(f"Point at address {addr} is not covered by any declared block")
        self.blocks = normalized_blocks if blocks is not None else None

        readiness_check = profile.get("readiness_check")
        if readiness_check is not None:
            if readiness_check["field"] not in names:
                raise ValueError("readiness_check.field must reference a defined point or computed field")
            if not readiness_check.get("allowed_values"):
                raise ValueError("readiness_check needs a non-empty allowed_values list")
        self.readiness_check = readiness_check

        self.device_id = config["device_id"]
        if type(self.device_id) is not int or not 1 <= self.device_id <= 247:
            raise ValueError("Modbus device_id must be 1..247; no broadcast")
        if client is None:
            from pymodbus.client import ModbusSerialClient
            client = ModbusSerialClient(port=config["port"], baudrate=config["baudrate"],
                                        parity=config["parity"], stopbits=config["stopbits"],
                                        bytesize=8, timeout=1, retries=1)
        self.client = client

    def _read_registers(self, function, address, count):
        fn = self.client.read_holding_registers if function == 3 else self.client.read_input_registers
        response = fn(address, count=count, device_id=self.device_id)
        if response.isError() or len(response.registers) != count:
            raise OSError("modbus_read_failed")
        result = {}
        for offset, raw in enumerate(response.registers):
            if not 0 <= raw <= 65535:
                raise ValueError("invalid_register")
            result[address + offset] = raw
        return result

    def read(self):
        if not self.client.connect():
            raise OSError("serial_unavailable")
        registers = {}
        if self.blocks is not None:
            for block in self.blocks:
                registers.update(self._read_registers(block["function"], block["start"], block["length"]))
        else:
            # v0.1 fallback: un apel Modbus per punct, fara grupare in blocuri.
            for p in self.points:
                count = 1 if p["encoding"] in ("u16", "s16") else 2
                registers.update(self._read_registers(p["function"], p["address"], count))

        values = {p["field"]: _decode_point(p, registers) for p in self.points}
        for c in self.computed:
            values[c["field"]] = sum(values[term] for term in c["sum_of"])
        return values

    def check_ready(self):
        """O singura citire, INAINTE de a incepe eșantionarea periodica, ca sa
        refuze un profil incompatibil devreme -- fara nicio scanare de
        adrese/baudrate, doar registrele deja declarate in profil."""
        values = self.read()
        if self.readiness_check is not None:
            value = values.get(self.readiness_check["field"])
            if value not in self.readiness_check["allowed_values"]:
                raise ValueError(f"incompatible_profile: {self.readiness_check['field']}={value!r}")

    def close(self):
        self.client.close()


class DisabledReader:
    """Enrollment/heartbeat-only mode used before an audited profile exists."""

    simulated = False
    telemetry_available = False
    kind = "disabled"

    def read(self):
        raise RuntimeError("telemetry_reader_disabled")

    def check_ready(self):
        pass

    def close(self):
        pass
