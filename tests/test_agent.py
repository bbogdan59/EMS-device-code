import json
import sqlite3
import sys
import uuid
from types import SimpleNamespace
import httpx
import pytest
from ems_device import cli as cli_module
from ems_device.agent import Agent
from ems_device.api import API
from ems_device.cli import _clock_sync_status, _retry_delay
from ems_device.provisioning import accept_enrollment_response, enrollment_payload
from ems_device.readers import DisabledReader, ModbusReader, Simulator
from ems_device.state import State

CREDS = {'device_id': str(uuid.uuid4()), 'station_id': str(uuid.uuid4()), 'credential_secret': 'test-only'}


def test_persistence_and_duplicate_receipt(tmp_path):
    state = State(tmp_path)
    identity = state.get('identity')
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'accepted': 0, 'duplicates': 1, 'rejected': 0})))
    Agent(state, api, Simulator()).sample()
    payload = state.pending()[0][1]
    state.close()
    state = State(tmp_path)
    assert state.get('identity') == identity
    assert state.pending()[0][1] == payload
    Agent(state, api, Simulator()).upload()
    assert state.pending() == []
    assert (tmp_path / 'agent.sqlite').stat().st_mode & 0o777 == 0o600
    state.close(); api.close()


@pytest.mark.parametrize('receipt', [
    {'accepted': 0, 'duplicates': 0, 'rejected': 1},
    {'accepted': 0, 'duplicates': 0, 'rejected': 0},
    {'accepted': True, 'duplicates': 0, 'rejected': 0},
])
def test_ambiguous_receipt_preserves_queue(tmp_path, receipt):
    state = State(tmp_path)
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=receipt)))
    agent = Agent(state, api, Simulator()); agent.sample()
    with pytest.raises(ValueError): agent.upload()
    assert len(state.pending()) == 1
    state.close(); api.close()


def test_per_item_receipt_retries_only_retryable_and_quarantines_permanent(tmp_path):
    state = State(tmp_path)
    for sequence in (1, 2, 3, 4):
        state.enqueue({'boot_id': 'per-item', 'sequence': sequence, 'measured_at': '2026-01-01T00:00:00+00:00'})
    response = {
        'accepted': 1,
        'duplicates': 1,
        'rejected': 2,
        'errors': ['legacy messages remain ignored by the new client'],
        'results': [
            {'boot_id': 'per-item', 'sequence': 1, 'status': 'accepted', 'retryable': False, 'reason_code': None},
            {'boot_id': 'per-item', 'sequence': 2, 'status': 'duplicate', 'retryable': False, 'reason_code': None},
            {'boot_id': 'per-item', 'sequence': 3, 'status': 'rejected', 'retryable': True, 'reason_code': 'future_timestamp'},
            {'boot_id': 'per-item', 'sequence': 4, 'status': 'rejected', 'retryable': False, 'reason_code': 'timestamp_too_old'},
        ],
    }
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response)
    ))

    Agent(state, api, Simulator()).upload()

    assert [item['sequence'] for _, item in state.pending()] == [3]
    dead_letters = state.dead_letters()
    assert len(dead_letters) == 1
    assert {key: dead_letters[0][key] for key in ('boot_id', 'sequence', 'reason_code')} == {
        'boot_id': 'per-item', 'sequence': 4, 'reason_code': 'timestamp_too_old'
    }
    assert state.health_snapshot(agent_version='test', clock_sync='unknown')['dead_letter_count'] == 1
    state.close(); api.close()


@pytest.mark.parametrize('mutate', [
    lambda results: results.pop(),
    lambda results: results[0].update(sequence=999),
    lambda results: results[0].update(status='accepted', retryable=True),
])
def test_malformed_per_item_receipt_preserves_entire_queue(tmp_path, mutate):
    state = State(tmp_path)
    state.enqueue({'boot_id': 'safe', 'sequence': 1})
    results = [{'boot_id': 'safe', 'sequence': 1, 'status': 'accepted', 'retryable': False, 'reason_code': None}]
    mutate(results)
    response = {'accepted': 1, 'duplicates': 0, 'rejected': 0, 'errors': [], 'results': results}
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response)
    ))

    with pytest.raises((TypeError, ValueError)):
        Agent(state, api, Simulator()).upload()

    assert len(state.pending()) == 1
    assert state.dead_letters() == []
    state.close(); api.close()


def test_receipt_aggregate_mismatch_preserves_entire_queue(tmp_path):
    state = State(tmp_path)
    state.enqueue({'boot_id': 'safe', 'sequence': 1})
    response = {
        'accepted': 0, 'duplicates': 1, 'rejected': 0, 'errors': [],
        'results': [{'boot_id': 'safe', 'sequence': 1, 'status': 'accepted', 'retryable': False, 'reason_code': None}],
    }
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response)
    ))
    with pytest.raises(ValueError, match='aggregate_mismatch'):
        Agent(state, api, Simulator()).upload()
    assert len(state.pending()) == 1
    state.close(); api.close()


def test_network_error_preserves_queue(tmp_path):
    def fail(request): raise httpx.ConnectError('offline')
    state = State(tmp_path)
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(fail))
    agent = Agent(state, api, Simulator()); agent.sample()
    with pytest.raises(httpx.ConnectError): agent.upload()
    assert len(state.pending()) == 1
    assert state.health_snapshot(agent_version='test', clock_sync='unknown')['upload_errors'] == 1
    state.close(); api.close()


def test_bounded_queue_preserves_oldest(tmp_path):
    state = State(tmp_path, capacity=1)
    state.enqueue({'sequence': 1})
    with pytest.raises(BufferError): state.enqueue({'sequence': 2})
    assert state.pending()[0][1]['sequence'] == 1
    assert state.health_snapshot(agent_version='test', clock_sync='unknown')['refused_samples'] == 1
    state.close()


def test_health_snapshot_reports_backlog_storage_and_unknowns(tmp_path):
    state = State(tmp_path, capacity=4)
    state.enqueue({'sequence': 1, 'measured_at': '2026-01-01T00:00:00+00:00'})
    state.enqueue({'sequence': 2, 'measured_at': '2026-01-01T00:00:10+00:00'})
    snapshot = state.health_snapshot(agent_version='0.test', clock_sync='unknown')
    assert snapshot['agent_version'] == '0.test'
    assert snapshot['clock_sync'] == 'unknown'
    assert snapshot['database_ok'] is True
    assert snapshot['outbox']['backlog'] == 2
    assert snapshot['outbox']['capacity'] == 4
    assert snapshot['outbox']['utilization_percent'] == 50.0
    assert snapshot['outbox']['oldest_measured_at'] == '2026-01-01T00:00:00+00:00'
    assert snapshot['outbox']['newest_measured_at'] == '2026-01-01T00:00:10+00:00'
    assert snapshot['outbox']['storage_bytes'] > 0
    assert 'provisioning_secret' not in json.dumps(snapshot)
    for suffix in ('', '-wal', '-shm'):
        path = tmp_path / f'agent.sqlite{suffix}'
        if path.exists():
            assert path.stat().st_mode & 0o777 == 0o600
    state.close()


def test_invalid_outbox_capacity_is_rejected(tmp_path):
    with pytest.raises(ValueError, match='positive integer'):
        State(tmp_path, capacity=0)


def test_health_success_is_throttled_and_errors_are_persistent(tmp_path):
    from datetime import datetime, timedelta, timezone

    state = State(tmp_path)
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state.record_success('sample', now=first)
    state.record_success('sample', now=first + timedelta(seconds=10))
    assert state.health_snapshot(agent_version='test', clock_sync='unknown')['last_sample_at'] == first.isoformat()
    state.record_error('sample', 'serial_unavailable', rs485=True, now=first)
    state.close()

    reopened = State(tmp_path)
    snapshot = reopened.health_snapshot(agent_version='test', clock_sync='unknown')
    assert snapshot['sample_errors'] == 1
    assert snapshot['rs485_errors'] == 1
    assert snapshot['last_error_code'] == 'serial_unavailable'
    reopened.close()


def test_backward_clock_step_is_visible_and_does_not_freeze_timestamp(tmp_path):
    from datetime import datetime, timedelta, timezone

    state = State(tmp_path)
    first = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    state.record_success('sample', now=first)
    earlier = first - timedelta(minutes=5)
    state.record_success('sample', now=earlier)
    snapshot = state.health_snapshot(agent_version='test', clock_sync='unknown')
    assert snapshot['clock_regressions'] == 1
    assert snapshot['last_sample_at'] == earlier.isoformat()
    state.close()


def test_clock_sync_marker_is_conservative(tmp_path):
    marker = tmp_path / 'synchronized'
    assert _clock_sync_status(marker) == 'unknown'
    marker.touch()
    assert _clock_sync_status(marker) == 'synchronized'


def test_sync_contract_no_write(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers['Authorization'] == f"Bearer {CREDS['device_id']}.test-only"
        if request.url.path == '/api/v1/config':
            return httpx.Response(200, json={'station_id': CREDS['station_id'], 'execution_mode': 'live',
                                            'config_version': 1, 'preference_version': 1})
        assert request.url.path == '/api/v1/devices/heartbeat'
        heartbeat_body = json.loads(request.content)
        assert heartbeat_body['capabilities']['inverter_write'] is False
        assert isinstance(heartbeat_body['system_stats'], dict)  # never fabricated, but always present as a dict
        return httpx.Response(200, json={})
    state = State(tmp_path)
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(handler))
    Agent(state, api, Simulator()).sync()
    assert state.get('station_config')['execution_mode'] == 'live'
    assert len(requests) == 2
    state.close(); api.close()


def test_upload_logs_posts_drained_entries_and_clears_buffer(tmp_path):
    from ems_device.log_buffer import CompactLogBuffer
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'accepted': 1})
    state = State(tmp_path)
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(handler))
    buffer = CompactLogBuffer()
    buffer._buffer.append({'occurred_at': '2026-01-01T00:00:00+00:00', 'level': 'warning', 'code': 'x'})
    agent = Agent(state, api, Simulator(), buffer)
    agent.upload_logs()
    assert len(requests) == 1
    assert requests[0].url.path == '/api/v1/devices/logs'
    assert json.loads(requests[0].content) == {'entries': [
        {'occurred_at': '2026-01-01T00:00:00+00:00', 'level': 'warning', 'code': 'x'}
    ]}
    assert list(buffer._buffer) == []  # drained
    state.close(); api.close()


def test_upload_logs_noop_when_buffer_empty(tmp_path):
    from ems_device.log_buffer import CompactLogBuffer
    def handler(request):
        raise AssertionError('should not make a network call for an empty buffer')
    state = State(tmp_path)
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(handler))
    Agent(state, api, Simulator(), CompactLogBuffer()).upload_logs()
    state.close(); api.close()


def test_upload_logs_noop_when_no_buffer_configured(tmp_path):
    state = State(tmp_path)
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={})))
    Agent(state, api, Simulator()).upload_logs()  # log_buffer=None default; must not raise
    state.close(); api.close()


def test_upload_logs_swallows_failure_never_raises(tmp_path):
    from ems_device.log_buffer import CompactLogBuffer
    state = State(tmp_path)
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(
        lambda r: httpx.Response(503, json={})))
    buffer = CompactLogBuffer()
    buffer._buffer.append({'occurred_at': '2026-01-01T00:00:00+00:00', 'level': 'error', 'code': 'x'})
    Agent(state, api, Simulator(), buffer).upload_logs()  # must not raise, unlike upload()
    state.close(); api.close()


@pytest.mark.parametrize('url', ['http://example.com', 'https://user:secret@example.com', 'https://example.com/path', 'https://example.com?token=x'])
def test_reject_unsafe_origin(url):
    with pytest.raises(ValueError): API(url)


def profile(tmp_path, **changes):
    data = {'verified': True, 'model': 'TEST ONLY', 'firmware': 'test', 'source': 'unit fixture',
            'points': [{'field': 'grid_power_w', 'address': 12, 'function': 3, 'encoding': 's16', 'scale': 10}]}
    data.update(changes)
    path = tmp_path / 'profile.json'; path.write_text(json.dumps(data))
    return {'profile': str(path), 'device_id': 1}


def test_signed_register_read_only(tmp_path):
    class Fake:
        def connect(self): return True
        def read_holding_registers(self, address, *, count, device_id):
            assert (address, count, device_id) == (12, 1, 1)
            return SimpleNamespace(isError=lambda: False, registers=[65526])
    assert ModbusReader(profile(tmp_path), Fake()).read() == {'grid_power_w': -100}


def test_unverified_map_refused(tmp_path):
    with pytest.raises(ValueError): ModbusReader(profile(tmp_path, verified=False), object())


def test_bad_response_no_zero(tmp_path):
    class Fake:
        def connect(self): return True
        def read_holding_registers(self, *args, **kwargs): return SimpleNamespace(isError=lambda: True)
    with pytest.raises(OSError): ModbusReader(profile(tmp_path), Fake()).read()


def test_modbus_sample_failure_increments_rs485_health(tmp_path):
    class Fake:
        def connect(self): return False
        def close(self): pass

    state = State(tmp_path / 'state')
    reader = ModbusReader(profile(tmp_path), Fake())
    with pytest.raises(OSError):
        Agent(state, None, reader).sample()
    snapshot = state.health_snapshot(agent_version='test', clock_sync='unknown')
    assert snapshot['sample_errors'] == 1
    assert snapshot['rs485_errors'] == 1
    state.close()


def test_nan_not_enqueued(tmp_path):
    state = State(tmp_path)
    reader = SimpleNamespace(read=lambda: {'pv_power_w': float('nan')}, simulated=False)
    with pytest.raises(ValueError): Agent(state, None, reader).sample()
    assert state.pending() == []
    state.close()


def test_claim_request_has_no_bearer():
    def handler(request):
        assert request.url.path == '/api/v1/devices/claim'
        assert 'authorization' not in request.headers
        assert json.loads(request.content)['claim_code'] == 'test-code'
        return httpx.Response(201, json=CREDS)
    api = API('https://ems.example.com', transport=httpx.MockTransport(handler))
    assert api.call('POST', '/devices/claim', {'claim_code': 'test-code'}, authenticated=False) == CREDS
    api.close()


def test_wrong_station_config_not_cached(tmp_path):
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        'station_id': str(uuid.uuid4()), 'execution_mode': 'shadow', 'config_version': 1, 'preference_version': 1})))
    state = State(tmp_path)
    with pytest.raises(ValueError): Agent(state, api, Simulator()).sync()
    assert state.get('station_config') is None
    state.close(); api.close()


def test_factory_identity_is_unique_stable_and_keeps_secrets_separate(tmp_path):
    first = State(tmp_path / 'one')
    identity = first.identity()
    assert identity['serial_number'].startswith('EMS-')
    assert identity['activation_code'].startswith('ACT-')
    assert len(identity['provisioning_secret']) >= 32
    first.close()

    reopened = State(tmp_path / 'one')
    assert reopened.identity() == identity
    second = State(tmp_path / 'two')
    assert second.identity()['serial_number'] != identity['serial_number']
    assert second.identity()['provisioning_secret'] != identity['provisioning_secret']
    reopened.close(); second.close()


def test_legacy_installation_uuid_is_preserved_during_identity_migration(tmp_path):
    legacy = str(uuid.uuid4())
    state = State(tmp_path)
    state.db.execute("DELETE FROM settings")
    state.db.commit()
    state.set('identity', legacy)
    state.close()

    migrated = State(tmp_path)
    assert migrated.identity()['installation_uuid'] == legacy
    assert migrated.get('identity') == legacy
    migrated.close()


def test_enrollment_payload_has_serial_and_separate_proofs(tmp_path):
    state = State(tmp_path)
    identity = state.identity()
    payload = enrollment_payload(identity, {'device_name': 'Kitchen EMS', 'hardware_platform': 'pi4'})
    assert payload['installation_uuid'] == identity['installation_uuid']
    assert payload['serial_number'] == identity['serial_number']
    assert payload['activation_code'] == identity['activation_code']
    assert payload['provisioning_secret'] == identity['provisioning_secret']
    assert payload['hardware_info'] == {'device_name': 'Kitchen EMS', 'platform': 'pi4'}
    state.close()


def test_pending_enrollment_is_retryable_and_assignment_is_persisted(tmp_path):
    state = State(tmp_path)
    assert accept_enrollment_response(state, {'status': 'pending'}) == 'pending'
    assert state.get('credentials') is None

    response = {'status': 'assigned', **CREDS}
    assert accept_enrollment_response(state, response) == 'assigned'
    assert state.get('credentials') == CREDS
    assert state.get('activation_code') is None
    assert 'activation_code' not in state.identity()
    state.close()


def test_enrollment_uses_unauthenticated_endpoint():
    def handler(request):
        assert request.url.path == '/api/v1/devices/enroll'
        assert 'authorization' not in request.headers
        return httpx.Response(200, json={'status': 'pending'})
    api = API('https://ems.example.com', transport=httpx.MockTransport(handler))
    assert api.enroll({'installation_uuid': str(uuid.uuid4())}) == {'status': 'pending'}
    api.close()


def test_disabled_reader_reports_no_telemetry_capability(tmp_path):
    def handler(request):
        if request.url.path == '/api/v1/config':
            return httpx.Response(200, json={'station_id': CREDS['station_id'], 'execution_mode': 'shadow',
                                            'config_version': 1, 'preference_version': 1})
        body = json.loads(request.content)
        assert body['capabilities']['telemetry'] is False
        return httpx.Response(200, json={})
    state = State(tmp_path)
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(handler))
    Agent(state, api, DisabledReader()).sync()
    state.close(); api.close()


def test_retry_after_is_honored_and_bounded():
    assert _retry_delay(httpx.Response(503, headers={'Retry-After': '45'}), 1) == 45
    assert _retry_delay(httpx.Response(503, headers={'Retry-After': '9999'}), 1) == 300


# --- issue #4: storage-full resilience --------------------------------------


class _FlakyConnection:
    """Wraps a real sqlite3.Connection so `execute()` can be made to fail on
    demand. sqlite3.Connection is a C type: neither its class nor an
    instance's `execute` attribute can be monkeypatched directly (both raise
    TypeError/AttributeError -- tried first), so `state.db` is swapped for
    this proxy instead. `with state.db:` still demarcates a real transaction
    via the wrapped connection; only `execute()` calls made INSIDE that
    block are interceptable, which is exactly what's needed to simulate
    ENOSPC on a specific write."""

    def __init__(self, real, should_fail):
        self._real = real
        self._should_fail = should_fail

    def execute(self, sql, *args):
        if self._should_fail(sql):
            raise sqlite3.OperationalError('database or disk is full')
        return self._real.execute(sql, *args)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc_info):
        return self._real.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_disk_full_during_sample_does_not_corrupt_queue_or_crash(tmp_path):
    """Simulates SQLite returning ENOSPC ('database or disk is full') on the
    INSERT inside `enqueue`. No portable unit test can validate real SD-card/
    power-loss durability (see docs/PROTOCOL.md) -- this validates the
    SOFTWARE contract: a write failure here must not corrupt already-queued
    data or crash the agent process, and must recover cleanly once space is
    available again, matching how `cli.py`'s run loop treats any `sample()`
    failure (logged, loop continues) and how `Agent._record_error` itself
    tolerates a second failure while trying to record the first one."""
    state = State(tmp_path)
    reader = Simulator()
    calls = {'n': 0}

    def should_fail(sql):
        if sql.startswith('INSERT INTO outbox') and calls['n'] == 0:
            calls['n'] += 1
            return True
        return False

    state.db = _FlakyConnection(state.db, should_fail)
    with pytest.raises(sqlite3.OperationalError):
        Agent(state, None, reader).sample()
    assert state.pending() == []  # nothing corrupted; nothing partially written

    Agent(state, None, reader).sample()  # "disk space freed" -- calls['n'] already consumed
    assert len(state.pending()) == 1  # recovered cleanly, exactly one sample queued
    state.close()


def test_disk_full_while_recording_health_error_does_not_mask_original_error(tmp_path):
    """`_record_error` itself writes to the same (possibly full) database.
    Agent already wraps that write in its own try/except (see agent.py) --
    this pins down that a SECOND disk-full failure there is swallowed with a
    log warning, not raised in place of the original sample() error."""
    state = State(tmp_path)
    reader = SimpleNamespace(read=lambda: (_ for _ in ()).throw(OSError('rs485 gone')), simulated=False)
    state.db = _FlakyConnection(state.db, should_fail=lambda sql: True)

    with pytest.raises(OSError, match='rs485 gone'):
        Agent(state, None, reader).sample()
    state.close()


# --- issue #3: transfer / factory reset / credential rotation ---------------


def test_clear_assignment_keeps_identity_but_drops_binding(tmp_path):
    state = State(tmp_path)
    identity_before = state.identity()
    state.set('credentials', CREDS)
    state.set('enrollment_status', 'assigned')
    state.set('platform_origin', 'https://old.example.com')
    state.set('credential_rotation_pending', True)

    state.clear_assignment()

    assert state.get('credentials') is None
    assert state.get('enrollment_status') is None
    assert state.get('platform_origin') is None
    assert state.get('credential_rotation_pending') is False
    after = state.identity()
    assert after['installation_uuid'] == identity_before['installation_uuid']
    assert after['serial_number'] == identity_before['serial_number']
    assert after['provisioning_secret'] == identity_before['provisioning_secret']
    # A fresh activation code -- the old one is one-use and may be compromised.
    assert after['activation_code'] != identity_before['activation_code']
    assert after['activation_code'].startswith('ACT-')
    state.close()


def test_clear_assignment_allows_rebinding_to_a_different_platform(tmp_path):
    state = State(tmp_path)
    state.set('platform_origin', 'https://old.example.com')
    state.clear_assignment()
    # No exception: the old binding is gone, a new origin is free to be set.
    state.set('platform_origin', 'https://new.example.com')
    assert state.get('platform_origin') == 'https://new.example.com'
    state.close()


def test_factory_reset_issues_a_new_identity_and_wipes_queue(tmp_path):
    state = State(tmp_path)
    identity_before = state.identity()
    state.set('credentials', CREDS)
    state.enqueue({'boot_id': 'b', 'sequence': 1})
    state.db.execute(
        "INSERT INTO dead_letter(original_outbox_id,payload,reason_code,failed_at) VALUES (99,'{}','x','2026-01-01T00:00:00+00:00')"
    )
    state.db.commit()

    state.factory_reset()

    after = state.identity()
    assert after['installation_uuid'] != identity_before['installation_uuid']
    assert after['serial_number'] != identity_before['serial_number']
    assert after['provisioning_secret'] != identity_before['provisioning_secret']
    assert state.get('credentials') is None
    assert state.pending() == []
    assert state.dead_letters() == []
    snapshot = state.health_snapshot(agent_version='test', clock_sync='unknown')
    assert snapshot['outbox']['backlog'] == 0
    assert snapshot['dead_letter_count'] == 0
    state.close()


def test_health_snapshot_reports_enrollment_status_and_rotation_pending(tmp_path):
    state = State(tmp_path)
    snapshot = state.health_snapshot(agent_version='test', clock_sync='unknown')
    assert snapshot['enrollment_status'] is None
    assert snapshot['credential_rotation_pending'] is False
    state.set('enrollment_status', 'assigned')
    state.set('credential_rotation_pending', True)
    snapshot = state.health_snapshot(agent_version='test', clock_sync='unknown')
    assert snapshot['enrollment_status'] == 'assigned'
    assert snapshot['credential_rotation_pending'] is True
    state.close()


def test_assigned_response_refuses_to_overwrite_a_different_existing_assignment(tmp_path):
    state = State(tmp_path)
    state.set('credentials', CREDS)
    other_response = {'status': 'assigned', 'device_id': str(uuid.uuid4()), 'station_id': str(uuid.uuid4()),
                       'credential_secret': 'someone-elses-secret'}
    with pytest.raises(ValueError, match='assignment_identity_mismatch'):
        accept_enrollment_response(state, other_response)
    # Original assignment must be untouched.
    assert state.get('credentials') == CREDS
    state.close()


def test_revoked_enrollment_status_is_rejected_and_recorded(tmp_path):
    state = State(tmp_path)
    with pytest.raises(ValueError, match='revoked'):
        accept_enrollment_response(state, {'status': 'revoked'})
    assert state.get('enrollment_status') == 'revoked'
    assert state.get('credentials') is None
    state.close()


def test_assigned_response_without_secret_and_no_existing_credentials_is_rejected(tmp_path):
    state = State(tmp_path)
    response = {'status': 'assigned', 'device_id': str(uuid.uuid4()), 'station_id': str(uuid.uuid4())}
    with pytest.raises(ValueError, match='no bootstrap credential'):
        accept_enrollment_response(state, response)
    state.close()


def test_rotate_credential_api_call_shape():
    def handler(request):
        assert request.url.path == '/api/v1/devices/credentials/rotate'
        assert request.headers['Authorization'] == f"Bearer {CREDS['device_id']}.test-only"
        assert json.loads(request.content) == {}
        return httpx.Response(200, json={'device_id': CREDS['device_id'], 'credential_secret': 'rotated-secret'})
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(handler))
    assert api.rotate_credential() == {'device_id': CREDS['device_id'], 'credential_secret': 'rotated-secret'}
    api.close()


# --- issue #3: cli.main() end-to-end, mocked HTTP transport -----------------


def _write_config(tmp_path, **overrides):
    settings = {'platform_url': 'https://ems.example.com', 'state_dir': str(tmp_path / 'state'), 'reader': 'disabled'}
    settings.update(overrides)
    lines = []
    for key, value in settings.items():
        if isinstance(value, bool):
            lines.append(f'{key} = {"true" if value else "false"}')
        elif isinstance(value, (int, float)):
            lines.append(f'{key} = {value}')
        else:
            lines.append(f'{key} = "{value}"')
    path = tmp_path / 'config.toml'
    path.write_text('\n'.join(lines))
    return path


def _run_cli(monkeypatch, config_path, action, *args, handler=None):
    if handler is not None:
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            cli_module, 'API',
            lambda base_url, credentials=None, **kw: API(base_url, credentials, transport=transport),
        )
    monkeypatch.setattr(sys, 'argv', ['ems-device', '--config', str(config_path), action, *args])
    cli_module.main()


def test_identity_diagnostic_does_not_compete_with_running_agent_lock(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    State(tmp_path / 'state').close()

    def unexpected_lock(*args, **kwargs):
        raise AssertionError("read-only identity must not request the exclusive agent lock")

    monkeypatch.setattr(cli_module.fcntl, 'flock', unexpected_lock)
    _run_cli(monkeypatch, config_path, 'identity')

    assert 'serial_number=' in capsys.readouterr().out


def test_cli_reset_requires_exact_serial_confirmation(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    state = State(tmp_path / 'state')
    real_serial = state.identity()['serial_number']
    state.set('credentials', CREDS)
    state.close()

    with pytest.raises(SystemExit, match='Refused'):
        _run_cli(monkeypatch, config_path, 'reset', '--confirm-serial', 'WRONG-SERIAL')

    reopened = State(tmp_path / 'state')
    assert reopened.get('credentials') == CREDS  # untouched
    assert reopened.identity()['serial_number'] == real_serial
    reopened.close()


def test_cli_reset_clears_assignment_when_serial_confirmed(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    state = State(tmp_path / 'state')
    identity = state.identity()
    state.set('credentials', CREDS)
    state.close()

    _run_cli(monkeypatch, config_path, 'reset', '--confirm-serial', identity['serial_number'])

    reopened = State(tmp_path / 'state')
    assert reopened.get('credentials') is None
    assert reopened.identity()['serial_number'] == identity['serial_number']
    assert reopened.identity()['activation_code'] != identity['activation_code']
    reopened.close()
    out = capsys.readouterr().out
    assert 'Assignment cleared' in out
    assert identity['activation_code'] not in out  # old (now-invalid) secret never printed


def test_cli_reset_factory_issues_new_identity(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    state = State(tmp_path / 'state')
    identity = state.identity()
    state.close()

    _run_cli(monkeypatch, config_path, 'reset', '--factory', '--confirm-serial', identity['serial_number'])

    reopened = State(tmp_path / 'state')
    assert reopened.identity()['serial_number'] != identity['serial_number']
    reopened.close()


def test_cli_rotate_credential_persists_new_secret(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    state = State(tmp_path / 'state')
    state.set('credentials', CREDS)
    state.set('platform_origin', 'https://ems.example.com')
    state.close()

    def handler(request):
        assert request.url.path == '/api/v1/devices/credentials/rotate'
        return httpx.Response(200, json={'credential_secret': 'brand-new-secret'})

    _run_cli(monkeypatch, config_path, 'rotate-credential', handler=handler)

    reopened = State(tmp_path / 'state')
    assert reopened.get('credentials')['credential_secret'] == 'brand-new-secret'
    assert reopened.get('credentials')['device_id'] == CREDS['device_id']
    assert reopened.get('credential_rotation_pending') is False
    reopened.close()


def test_cli_rotate_credential_leaves_pending_flag_set_on_network_failure(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    state = State(tmp_path / 'state')
    state.set('credentials', CREDS)
    state.set('platform_origin', 'https://ems.example.com')
    state.close()

    def handler(request):
        raise httpx.ConnectError('offline')

    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, config_path, 'rotate-credential', handler=handler)

    reopened = State(tmp_path / 'state')
    # Outcome unknown (request may have reached the server before the drop):
    # the flag survives so 'health' surfaces the ambiguity instead of hiding it.
    assert reopened.get('credential_rotation_pending') is True
    assert reopened.get('credentials') == CREDS  # old secret still what we have locally
    reopened.close()


def test_cli_rotate_credential_without_credentials_refuses(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    State(tmp_path / 'state').close()
    with pytest.raises(SystemExit, match='No active credentials'):
        _run_cli(monkeypatch, config_path, 'rotate-credential')


def test_cli_run_raises_distinct_error_type_on_401_without_leaking_message(tmp_path, monkeypatch, caplog):
    config_path = _write_config(tmp_path)
    state = State(tmp_path / 'state')
    state.set('credentials', CREDS)
    state.set('platform_origin', 'https://ems.example.com')
    state.close()

    def handler(request):
        return httpx.Response(401, json={'detail': 'revoked'})

    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, config_path, 'run', '--once', handler=handler)

    # Only the exception TYPE name is logged -- never text that could carry
    # a token or response body (see cli.py's outer handler docstring/comment).
    assert 'CredentialInactiveError' in caplog.text
    assert 'revoked' not in caplog.text
    assert CREDS['credential_secret'] not in caplog.text
