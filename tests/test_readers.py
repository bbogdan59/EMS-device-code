"""Tests for issue #1: u32/word-order/offset decoding, grouped block reads,
computed fields, and the pre-loop readiness check. Synthetic register values
only -- these test the DECODING MECHANISM, not the shipped candidate profile
against real hardware (see docs/VALIDATION_SG04LP3.md for that gap)."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ems_device.readers import FIELDS, MAX_BLOCK_REGISTERS, ModbusReader
from ems_device.agent import Agent
from ems_device.state import State


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


def test_block_cannot_bridge_undeclared_register(tmp_path):
    profile = base_profile(
        points=[
            {"field": "pv1_power_w", "address": 12, "function": 3, "encoding": "u16", "scale": 1},
            {"field": "pv2_power_w", "address": 14, "function": 3, "encoding": "u16", "scale": 1},
        ],
        blocks=[{"function": 3, "start": 12, "length": 3}],
    )
    fake = FakeSerial({})
    with pytest.raises(ValueError, match="undeclared registers"):
        ModbusReader(write_profile(tmp_path, profile), fake)
    assert fake.calls == []


@pytest.mark.parametrize("blocked", [False, True])
def test_holding_and_input_registers_at_same_address_stay_independent(tmp_path, blocked):
    class SeparateAddressSpaces(FakeSerial):
        def read_input_registers(self, address, *, count, device_id):
            assert (address, count, device_id) == (12, 1, 1)
            return SimpleNamespace(isError=lambda: False, registers=[200])

    profile = base_profile(points=[
        {"field": "pv1_power_w", "address": 12, "function": 3, "encoding": "u16", "scale": 1},
        {"field": "pv2_power_w", "address": 12, "function": 4, "encoding": "u16", "scale": 1},
    ])
    if blocked:
        profile["blocks"] = [{"function": function, "start": 12, "length": 1} for function in (3, 4)]
    reader = ModbusReader(write_profile(tmp_path, profile), SeparateAddressSpaces({12: 100}))
    assert reader.read() == {"pv1_power_w": 100, "pv2_power_w": 200}


@pytest.mark.parametrize("encoding,order,registers,sentinel", [
    ("u16", None, [65535], 65535),
    ("s16", None, [65535], 65535),
    ("u32", "low_high", [0x1234, 0xFFFF], 0xFFFF1234),
    ("s32", "low_high", [0x1234, 0xFFFF], 0xFFFF1234),
    ("u32", "high_low", [0xFFFF, 0x1234], 0xFFFF1234),
    ("s32", "high_low", [0xFFFF, 0x1234], 0xFFFF1234),
])
def test_unavailable_values_checked_before_sign_scale_offset(tmp_path, encoding, order, registers, sentinel):
    profile = base_profile(points=[
        {"field": "battery_power_w", "address": 12, "function": 3, "encoding": encoding,
         "word_order": order, "scale": -2, "offset": 10, "unavailable_values": [sentinel]},
        {"field": "pv_power_w", "address": 20, "function": 3, "encoding": "u16", "scale": 1},
    ])
    raw = {12 + index: value for index, value in enumerate(registers)}
    raw[20] = 0
    reader = ModbusReader(write_profile(tmp_path, profile), FakeSerial(raw))
    assert reader.read() == {"pv_power_w": 0}
    assert reader.unavailable_fields == ["battery_power_w"]


def test_no_implicit_unavailable_sentinel(tmp_path):
    reader = ModbusReader(write_profile(tmp_path, base_profile()), FakeSerial({12: 65535}))
    assert reader.read() == {"grid_power_w": -10}
    assert reader.unavailable_fields == []


def test_partial_sample_omits_missing_total_and_preserves_zero_then_recovers(tmp_path):
    profile = base_profile(
        points=[
            {"field": "pv1_power_w", "address": 12, "function": 3, "encoding": "u16", "scale": 1},
            {"field": "pv2_power_w", "address": 13, "function": 3, "encoding": "u16", "scale": 1,
             "unavailable_values": [65535]},
        ],
        computed=[{"field": "pv_power_w", "sum_of": ["pv1_power_w", "pv2_power_w"]}],
    )
    fake = FakeSerial({12: 0, 13: 65535})
    reader = ModbusReader(write_profile(tmp_path, profile), fake)
    state = State(tmp_path / "state")
    try:
        agent = Agent(state, None, reader)
        agent.sample()
        payload = state.pending()[0][1]
        assert payload["pv1_power_w"] == 0
        assert "pv2_power_w" not in payload and "pv_power_w" not in payload
        assert payload["quality_flags"] == {
            "simulated": False, "unavailable_fields": ["pv2_power_w", "pv_power_w"],
        }
        fake.register_values[13] = 100
        agent.sample()
        recovered = state.pending()[1][1]
        assert recovered["pv_power_w"] == 100
        assert recovered["pv2_power_w"] == 100
        assert recovered["quality_flags"] == {"simulated": False}
        # The original queued observation does not change on recovery.
        assert state.pending()[0][1] == payload
    finally:
        state.close()


def test_all_unavailable_sample_is_not_queued_as_zero(tmp_path):
    profile = base_profile()
    profile["points"][0]["unavailable_values"] = [65535]
    reader = ModbusReader(write_profile(tmp_path, profile), FakeSerial({12: 65535}))
    state = State(tmp_path / "state")
    try:
        with pytest.raises(ValueError, match="invalid_telemetry_fields"):
            Agent(state, None, reader).sample()
        assert state.pending() == []
    finally:
        state.close()


@pytest.mark.parametrize("unavailable", [[-1], [65536], [True], [1.0], "65535", [0] * 33])
def test_invalid_unavailable_values_rejected_before_bus_access(tmp_path, unavailable):
    profile = base_profile()
    profile["points"][0]["unavailable_values"] = unavailable
    fake = FakeSerial({})
    with pytest.raises(ValueError, match="unavailable_values"):
        ModbusReader(write_profile(tmp_path, profile), fake)
    assert fake.calls == []


@pytest.mark.parametrize("raw", [True, 1.5, -1, 65536])
def test_invalid_raw_register_rejected(tmp_path, raw):
    reader = ModbusReader(write_profile(tmp_path, base_profile()), FakeSerial({12: raw}))
    with pytest.raises(ValueError, match="invalid_register"):
        reader.read()


@pytest.mark.parametrize("field,value", [("function", 3.0), ("scale", True), ("offset", False)])
def test_profile_numeric_types_are_strict(tmp_path, field, value):
    profile = base_profile()
    profile["points"][0][field] = value
    with pytest.raises(ValueError):
        ModbusReader(write_profile(tmp_path, profile), FakeSerial({}))


def test_computed_field_cannot_double_count_a_point(tmp_path):
    profile = base_profile(computed=[{"field": "pv_power_w", "sum_of": ["grid_power_w", "grid_power_w"]}])
    with pytest.raises(ValueError, match="computed.sum_of"):
        ModbusReader(write_profile(tmp_path, profile), FakeSerial({}))


def test_readiness_only_reads_required_points_even_with_blocks(tmp_path):
    profile = base_profile(
        points=[
            {"field": "inverter_status_code", "address": 500, "function": 3, "encoding": "u16", "scale": 1},
            {"field": "pv_power_w", "address": 501, "function": 3, "encoding": "u16", "scale": 1},
        ],
        blocks=[{"function": 3, "start": 500, "length": 2}],
        readiness_check={"field": "inverter_status_code", "allowed_values": [2]},
    )
    fake = FakeSerial({500: 2})  # Reading telemetry at 501 would fail.
    ModbusReader(write_profile(tmp_path, profile), fake).check_ready()
    assert fake.calls == [(500, 1, 1)]


@pytest.mark.parametrize("allowed", [[None], [True], [float("nan")], [float("inf")]])
def test_readiness_allowed_values_must_be_finite_numbers(tmp_path, allowed):
    profile = base_profile(readiness_check={"field": "grid_power_w", "allowed_values": allowed})
    with pytest.raises(ValueError, match="allowed_values"):
        ModbusReader(write_profile(tmp_path, profile), FakeSerial({}))


def test_readiness_rejects_unavailable_computed_value(tmp_path):
    profile = base_profile(
        points=[
            {"field": "pv1_power_w", "address": 12, "function": 3, "encoding": "u16", "scale": 1},
            {"field": "pv2_power_w", "address": 13, "function": 3, "encoding": "u16", "scale": 1,
             "unavailable_values": [65535]},
        ],
        computed=[{"field": "pv_power_w", "sum_of": ["pv1_power_w", "pv2_power_w"]}],
        readiness_check={"field": "pv_power_w", "allowed_values": [100]},
    )
    fake = FakeSerial({12: 100, 13: 65535})
    reader = ModbusReader(write_profile(tmp_path, profile), fake)
    with pytest.raises(ValueError, match="incompatible_profile"):
        reader.check_ready()
    fake.register_values[13] = 0
    reader.check_ready()


@pytest.mark.parametrize("method", ["check_ready", "read"])
def test_identity_mismatch_prevents_any_telemetry_read(tmp_path, method):
    profile = base_profile(identity_checks=[{"function": 3, "address": 100, "expected_registers": [42, 7]}])
    fake = FakeSerial({100: 42, 101: 8})
    reader = ModbusReader(write_profile(tmp_path, profile), fake)
    with pytest.raises(ValueError, match="incompatible_identity_registers"):
        getattr(reader, method)()
    assert fake.calls == [(100, 2, 1)]


@pytest.mark.parametrize("interruption", ["read_error", "reconnect", "close"])
def test_identity_is_rechecked_after_connection_interruption(tmp_path, interruption):
    class ReconnectingSerial(FakeSerial):
        def connect(self):
            self.connected = True
            return True

        def close(self):
            self.connected = False

    profile = base_profile(identity_checks=[{"function": 4, "address": 100, "expected_registers": [42]}])
    fake = ReconnectingSerial({100: 42, 12: 5})
    reader = ModbusReader(write_profile(tmp_path, profile), fake)
    reader.check_ready()
    assert fake.calls == [(100, 1, 1)]
    assert reader.read() == {"grid_power_w": 50}
    assert fake.calls == [(100, 1, 1), (12, 1, 1)]  # identity cached for this connection
    if interruption == "read_error":
        fake.register_values[12] = -1
        with pytest.raises(ValueError, match="invalid_register"):
            reader.read()
    elif interruption == "reconnect":
        fake.connected = False
    else:
        reader.close()
    fake.register_values.update({100: 99, 12: 5})
    fake.calls.clear()
    with pytest.raises(ValueError, match="incompatible_identity_registers"):
        reader.read()
    assert fake.calls == [(100, 1, 1)]


@pytest.mark.parametrize("check", [
    {"function": 6, "address": 100, "expected_registers": [42]},
    {"function": 3, "address": 65535, "expected_registers": [42, 7]},
    {"function": 3, "address": 100, "expected_registers": []},
    {"function": 3, "address": 100, "expected_registers": [True]},
    {"function": 3, "address": 100, "expected_registers": [65536]},
    {"function": 3, "address": 100, "expected_registers": [42], "write": True},
])
def test_invalid_identity_check_rejected_before_bus_access(tmp_path, check):
    fake = FakeSerial({})
    with pytest.raises(ValueError, match="identity check"):
        ModbusReader(write_profile(tmp_path, base_profile(identity_checks=[check])), fake)
    assert fake.calls == []


@pytest.mark.parametrize("method", ["read", "check_ready"])
def test_cleanup_failure_preserves_original_connection_error(tmp_path, method):
    class BrokenSerial(FakeSerial):
        def close(self):
            raise RuntimeError("cleanup_failed")

    reader = ModbusReader(write_profile(tmp_path, base_profile()), BrokenSerial({}, connected=False))
    with pytest.raises(OSError, match="serial_unavailable"):
        getattr(reader, method)()
