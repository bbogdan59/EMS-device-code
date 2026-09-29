from importlib.metadata import version
import json

import httpx

from ems_device import __version__, inventory
from ems_device.agent import Agent
from ems_device.api import API
from ems_device.provisioning import enrollment_payload
from ems_device.readers import DisabledReader
from ems_device.state import State


def test_installed_version_comes_from_package_metadata():
    assert __version__ == version('ems-device')


def test_pending_and_assigned_inventory_match(monkeypatch, tmp_path):
    monkeypatch.setattr(inventory.platform, 'machine', lambda: 'aarch64')
    monkeypatch.setattr(inventory.platform, 'freedesktop_os_release',
                        lambda: {'ID': 'debian', 'VERSION_ID': '12'})
    (tmp_path / 'build_id.txt').write_text('a' * 40)
    monkeypatch.setattr(inventory, 'files', lambda package: tmp_path)
    settings = {'hardware_platform': 'raspberry-pi-4'}
    state = State(tmp_path / 'state')
    pending = enrollment_payload(state.identity(), settings)
    creds = {'device_id': 'device', 'station_id': 'station', 'credential_secret': 'secret'}
    heartbeats = []
    def handler(request):
        if request.url.path.endswith('/config'):
            return httpx.Response(200, json={'station_id': 'station', 'execution_mode': 'shadow',
                                            'config_version': 1, 'preference_version': 1})
        heartbeats.append(json.loads(request.content))
        return httpx.Response(200, json={})
    api = API('https://ems.example.com', creds, transport=httpx.MockTransport(handler))
    Agent(state, api, DisabledReader(), settings=settings).sync()
    heartbeat = heartbeats[0]
    assert pending['agent_version'] == heartbeat['firmware_version'] == __version__
    for key in ('build_id', 'hardware_platform', 'architecture', 'os_version'):
        assert pending[key] == heartbeat[key]
    assert heartbeat['build_id'] == 'a' * 40
    assert heartbeat['architecture'] == 'aarch64'
    assert heartbeat['os_version'] == 'debian 12'
    assert heartbeat['capabilities']['inverter_write'] is False
    api.close()
    state.close()


def test_unknown_inventory_is_omitted(monkeypatch, tmp_path):
    monkeypatch.setattr(inventory, '__version__', None)
    monkeypatch.setattr(inventory, 'files', lambda package: tmp_path)
    monkeypatch.setattr(inventory.platform, 'machine', lambda: '')
    def unknown_os():
        raise OSError('unavailable')
    monkeypatch.setattr(inventory.platform, 'freedesktop_os_release', unknown_os)
    assert inventory.snapshot() == {}
    (tmp_path / 'build_id.txt').write_text('unknown-version')
    assert inventory.snapshot() == {}
