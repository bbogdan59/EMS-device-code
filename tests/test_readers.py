"""Tests for issue #1: u32/word-order/offset decoding, grouped block reads,
computed fields, and the pre-loop readiness check. Synthetic register values
only -- these test the DECODING MECHANISM, not the shipped candidate profile
against real hardware (see docs/VALIDATION_SG04LP3.md for that gap)."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ems_device.readers import FIELDS, MAX_BLOCK_REGISTERS, ModbusReader


def base_profile(**changes):
    data = {
        "verified": True, "model": "TEST ONLY", "firmware": "test", "source": "unit fixture",
        "points": [{"field": "grid_power_w", "address": 12, "function": 3, "encoding": "s16", "scale": 10}],
    }
    data.update(changes)
    return data


def write_profile(tmp_path, data):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data))
    return {"profile": str(path), "device_id": 1}


class FakeSerial:
    """Records each Modbus call and answers from a static address->raw map."""

    def __init__(self, register_values, connected=True):
        self.register_values = register_values
        self.connected = connected
        self.calls = []

    def connect(self):
        return self.connected

    def close(self):
        pass

    def read_holding_registers(self, address, *, count, device_id):
        return self._read(address, count, device_id)

    def read_input_registers(self, address, *, count, device_id):
        return self._read(address, count, device_id)

    def _read(self, address, count, device_id):
        self.calls.append((address, count, device_id))
        values = [self.register_values[a] for a in range(address, address + count)]
        return SimpleNamespace(isError=lambda: False, registers=values)


def test_u32_low_high_word_order_matches_deye_energy_counters(tmp_path):
    # 12345.6 kWh at scale 0.1 -> raw 123456 = 0x0001E240; low word 0xE240, high word 0x0001.
    profile = base_profile(points=[
        {"field": "pv_energy_total_kwh", "address": 534, "function": 3, "encoding": "u32",
         "word_order": "low_high", "scale": 0.1},
    ])
    fake = FakeSerial({534: 0xE240, 535: 0x0001})
    assert ModbusReader(write_profile(tmp_path, profile), fake).read() == {"pv_energy_total_kwh": pytest.approx(12345.6)}


def test_u32_high_low_word_order(tmp_path):
    profile = base_profile(points=[
        {"field": "pv_energy_total_kwh", "address": 534, "function": 3, "encoding": "u32",
         "word_order": "high_low", "scale": 0.1},
    ])
    fake = FakeSerial({534: 0x0001, 535: 0xE240})
    assert ModbusReader(write_profile(tmp_path, profile), fake).read() == {"pv_energy_total_kwh": pytest.approx(12345.6)}


def test_s32_word_order_decodes_negative(tmp_path):
    # -100 as s32 -> 0xFFFFFF9C; low word 0xFF9C, high word 0xFFFF.
    profile = base_profile(points=[
        {"field": "grid_export_energy_total_kwh", "address": 100, "function": 3, "encoding": "s32",
         "word_order": "low_high", "scale": 1},
    ])
    fake = FakeSerial({100: 0xFF9C, 101: 0xFFFF})
    assert ModbusReader(write_profile(tmp_path, profile), fake).read() == {"grid_export_energy_total_kwh": -100}


def test_offset_applied_after_scale(tmp_path):
    # Deye temperature convention: raw*0.1 - 100. raw=900 -> -10.0 C.
    profile = base_profile(points=[
        {"field": "battery_temperature_c", "address": 586, "function": 3, "encoding": "u16",
         "scale": 0.1, "offset": -100.0},
    ])
    fake = FakeSerial({586: 900})
    assert ModbusReader(write_profile(tmp_path, profile), fake).read() == {"battery_temperature_c": pytest.approx(-10.0)}


def test_32bit_point_requires_word_order(tmp_path):
    profile = base_profile(points=[
        {"field": "pv_energy_total_kwh", "address": 534, "function": 3, "encoding": "u32", "scale": 0.1},
    ])
    with pytest.raises(ValueError, match="word_order"):
        ModbusReader(write_profile(tmp_path, profile), FakeSerial({}))


def test_block_read_issues_one_call_for_multiple_points(tmp_path):
    profile = base_profile(
        points=[
            {"field": "battery_voltage_v", "address": 587, "function": 3, "encoding": "u16", "scale": 0.01},
            {"field": "battery_soc_percent", "address": 588, "function": 3, "encoding": "u16", "scale": 1},
        ],
        blocks=[{"function": 3, "start": 587, "length": 2}],
    )
    fake = FakeSerial({587: 5320, 588: 77})
    reader = ModbusReader(write_profile(tmp_path, profile), fake)
    assert reader.read() == {"battery_voltage_v": pytest.approx(53.2), "battery_soc_percent": 77}
    assert fake.calls == [(587, 2, 1)]


def test_block_never_reads_past_declared_length(tmp_path):
    """Points 588 (soc) and 590 (power) have an undocumented gap at 589 --
    they must be two separate blocks, never one 3-register block spanning it."""
    profile = base_profile(
        points=[
            {"field": "battery_soc_percent", "address": 588, "function": 3, "encoding": "u16", "scale": 1},
            {"field": "battery_power_w", "address": 590, "function": 3, "encoding": "s16", "scale": 1},
        ],
        blocks=[
            {"function": 3, "start": 588, "length": 1},
            {"function": 3, "start": 590, "length": 1},
        ],
    )
    fake = FakeSerial({588: 80, 590: 500})
    reader = ModbusReader(write_profile(tmp_path, profile), fake)
    assert reader.read() == {"battery_soc_percent": 80, "battery_power_w": 500}
    assert sorted(fake.calls) == [(588, 1, 1), (590, 1, 1)]


def test_point_not_covered_by_any_block_is_rejected_at_construction(tmp_path):
    profile = base_profile(
        points=[{"field": "battery_soc_percent", "address": 588, "function": 3, "encoding": "u16", "scale": 1}],
        blocks=[{"function": 3, "start": 500, "length": 1}],
    )
    with pytest.raises(ValueError, match="not covered by any declared block"):
        ModbusReader(write_profile(tmp_path, profile), FakeSerial({}))


def test_overlapping_blocks_are_rejected(tmp_path):
    profile = base_profile(
        points=[{"field": "battery_soc_percent", "address": 588, "function": 3, "encoding": "u16", "scale": 1}],
        blocks=[
            {"function": 3, "start": 586, "length": 3},
            {"function": 3, "start": 588, "length": 2},
        ],
    )
    with pytest.raises(ValueError, match="must not overlap"):
        ModbusReader(write_profile(tmp_path, profile), FakeSerial({}))


def test_block_length_over_cap_is_rejected(tmp_path):
    profile = base_profile(
        points=[{"field": "battery_soc_percent", "address": 0, "function": 3, "encoding": "u16", "scale": 1}],
        blocks=[{"function": 3, "start": 0, "length": MAX_BLOCK_REGISTERS + 1}],
    )
    with pytest.raises(ValueError, match=f"1..{MAX_BLOCK_REGISTERS}"):
        ModbusReader(write_profile(tmp_path, profile), FakeSerial({}))


def test_computed_field_sums_referenced_points(tmp_path):
    profile = base_profile(
        points=[
            {"field": "pv1_power_w", "address": 672, "function": 3, "encoding": "u16", "scale": 1},
            {"field": "pv2_power_w", "address": 673, "function": 3, "encoding": "u16", "scale": 1},
        ],
        computed=[{"field": "pv_power_w", "sum_of": ["pv1_power_w", "pv2_power_w"]}],
    )
    fake = FakeSerial({672: 900, 673: 350})
    reader = ModbusReader(write_profile(tmp_path, profile), fake)
    assert reader.read() == {"pv1_power_w": 900, "pv2_power_w": 350, "pv_power_w": 1250}


def test_computed_field_referencing_unknown_point_is_rejected(tmp_path):
    profile = base_profile(
        points=[{"field": "pv1_power_w", "address": 672, "function": 3, "encoding": "u16", "scale": 1}],
        computed=[{"field": "pv_power_w", "sum_of": ["pv1_power_w", "pv2_power_w"]}],
    )
    with pytest.raises(ValueError, match="computed.sum_of"):
        ModbusReader(write_profile(tmp_path, profile), FakeSerial({}))


def test_readiness_check_passes_for_allowed_value(tmp_path):
    profile = base_profile(
        points=[{"field": "inverter_status_code", "address": 500, "function": 3, "encoding": "u16", "scale": 1}],
        readiness_check={"field": "inverter_status_code", "allowed_values": [0, 1, 2, 3, 4]},
    )
    fake = FakeSerial({500: 2})
    ModbusReader(write_profile(tmp_path, profile), fake).check_ready()  # does not raise


def test_readiness_check_rejects_unexpected_value(tmp_path):
    profile = base_profile(
        points=[{"field": "inverter_status_code", "address": 500, "function": 3, "encoding": "u16", "scale": 1}],
        readiness_check={"field": "inverter_status_code", "allowed_values": [0, 1, 2, 3, 4]},
    )
    fake = FakeSerial({500: 9999})
    with pytest.raises(ValueError, match="incompatible_profile"):
        ModbusReader(write_profile(tmp_path, profile), fake).check_ready()


def test_readiness_check_field_must_be_defined(tmp_path):
    profile = base_profile(readiness_check={"field": "unknown_field", "allowed_values": [1]})
    with pytest.raises(ValueError, match="readiness_check.field"):
        ModbusReader(write_profile(tmp_path, profile), FakeSerial({}))


def test_function_4_input_registers_supported_in_blocks(tmp_path):
    profile = base_profile(
        points=[{"field": "battery_soc_percent", "address": 588, "function": 4, "encoding": "u16", "scale": 1}],
        blocks=[{"function": 4, "start": 588, "length": 1}],
    )
    fake = FakeSerial({588: 42})
    assert ModbusReader(write_profile(tmp_path, profile), fake).read() == {"battery_soc_percent": 42}


def test_v01_fallback_reads_one_register_per_point_without_blocks(tmp_path):
    """No `blocks` key at all: behaves exactly like the original v0.1 reader."""
    profile = base_profile(points=[
        {"field": "pv_power_w", "address": 10, "function": 3, "encoding": "u16", "scale": 1},
        {"field": "load_power_w", "address": 20, "function": 3, "encoding": "u16", "scale": 1},
    ])
    fake = FakeSerial({10: 500, 20: 300})
    assert ModbusReader(write_profile(tmp_path, profile), fake).read() == {"pv_power_w": 500, "load_power_w": 300}
    assert sorted(fake.calls) == [(10, 1, 1), (20, 1, 1)]


def test_disconnected_client_fails_before_any_block_read(tmp_path):
    profile = base_profile(blocks=[{"function": 3, "start": 12, "length": 1}])
    fake = FakeSerial({12: 100}, connected=False)
    with pytest.raises(OSError):
        ModbusReader(write_profile(tmp_path, profile), fake).read()


def test_candidate_sg04lp3_profile_is_internally_consistent(tmp_path):
    """The shipped candidate profile (verified=false, pending operator hardware
    validation -- see docs/VALIDATION_SG04LP3.md) must still be a well-formed
    profile: every point covered by exactly one block, no unknown fields, no
    duplicate fields, computed references resolve. This is a schema/self
    -consistency check, NOT a hardware validation."""
    candidate_path = Path(__file__).resolve().parent.parent / "profiles" / "deye_sg04lp3_candidate.json"
    data = json.loads(candidate_path.read_text())
    assert data["verified"] is False, "candidate profile must not ship pre-marked verified=true"
    data["verified"] = True  # only this in-memory test copy; never the shipped file
    for point in data["points"]:
        assert point["field"] in FIELDS

    register_values = {}
    for block in data["blocks"]:
        for addr in range(block["start"], block["start"] + block["length"]):
            register_values[addr] = 1  # plausible non-zero placeholder; decoding correctness is tested above

    fake = FakeSerial(register_values)
    reader = ModbusReader(write_profile(tmp_path, data), fake)
    values = reader.read()
    for point in data["points"]:
        assert point["field"] in values
    reader.check_ready()  # status register decodes to 1 ("selfcheck"), an allowed value
