import json
import uuid
from types import SimpleNamespace
import httpx
import pytest
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
        assert json.loads(request.content)['capabilities']['inverter_write'] is False
        return httpx.Response(200, json={})
    state = State(tmp_path)
    api = API('https://ems.example.com', CREDS, transport=httpx.MockTransport(handler))
    Agent(state, api, Simulator()).sync()
    assert state.get('station_config')['execution_mode'] == 'live'
    assert len(requests) == 2
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
