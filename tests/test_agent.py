import json
import uuid
from types import SimpleNamespace
import httpx
import pytest
from ems_device.agent import Agent
from ems_device.api import API
from ems_device.readers import ModbusReader, Simulator
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
    state.close(); api.close()


def test_bounded_queue_preserves_oldest(tmp_path):
    state = State(tmp_path, capacity=1)
    state.enqueue({'sequence': 1})
    with pytest.raises(BufferError): state.enqueue({'sequence': 2})
    assert state.pending()[0][1]['sequence'] == 1
    state.close()


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
