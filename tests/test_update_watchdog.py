import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ems_device import runtime_health, update_watchdog as watchdog


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    install = tmp_path / 'opt'
    old, new = install / 'releases/old', install / 'releases/new'
    old.mkdir(parents=True)
    new.mkdir()
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'agent.sqlite').write_bytes(b'identity credentials outbox')
    (install / 'current').symlink_to(old)
    monkeypatch.setattr(watchdog, 'kernel_boot_id', lambda: 'kernel-1')
    monkeypatch.setattr(watchdog.time, 'monotonic', lambda: 100.0)
    monkeypatch.setattr(watchdog.time, 'sleep', lambda seconds: None)
    commands = []
    monkeypatch.setattr(watchdog, '_run', lambda command: commands.append(command))
    monkeypatch.setattr(watchdog, '_service_pid', lambda: 1234)
    watchdog.atomic_json(state / watchdog.HEALTH, {'boot_id': 'old-process'})
    record = watchdog.begin(install, new, old, '0.2.0', state)
    return install, state, record, commands


def activate(deployment):
    install, state, record, _ = deployment
    watchdog.replace_link(install / 'current', Path(record['release']))
    record['phase'] = 'awaiting_confirmation'
    watchdog.atomic_json(install / watchdog.JOURNAL, record)
    return install, state, record


def evidence(record, **overrides):
    return {'boot_id': 'new-process', 'pid': 1234, 'release': record['release'],
            'version': record['version'], 'attempt_id': record['attempt_id'],
            'kernel_boot_id': 'kernel-1', 'platform_contact': True,
            'observed_uptime': 100.0, **overrides}


def test_only_new_live_process_after_platform_contact_confirms(deployment):
    install, state, record = activate(deployment)
    watchdog.atomic_json(state / watchdog.HEALTH, evidence(record))
    assert watchdog.recover(install) == 'confirmed'
    saved = watchdog.read_json(install / watchdog.JOURNAL)
    assert saved['last_known_good'] == record['release']
    assert watchdog.recover(install) == 'confirmed'  # Idempotent timer ticks.
    assert (state / 'agent.sqlite').read_bytes() == b'identity credentials outbox'


@pytest.mark.parametrize('override', [
    {'boot_id': 'old-process'}, {'boot_id': ''}, {'version': '0.1.0'},
    {'attempt_id': 'replayed-attempt'}, {'kernel_boot_id': 'kernel-old'},
    {'release': '/opt/other'}, {'platform_contact': False},
    {'observed_uptime': 99}, {'observed_uptime': 101}, {'observed_uptime': float('nan')},
    {'pid': 9999}, {'pid': True},
])
def test_replayed_mismatched_or_dead_process_evidence_cannot_confirm(deployment, override):
    install, state, record = activate(deployment)
    # Deliberately allow invalid JSON numbers here: the privileged reader must
    # reject them as confirmation evidence even if a process wrote them.
    (state / watchdog.HEALTH).write_text(json.dumps(evidence(record, **override)))
    assert watchdog.recover(install) == 'awaiting_confirmation'
    assert watchdog.read_json(install / watchdog.JOURNAL)['phase'] != 'confirmed'


def test_timeout_recovers_previous_release_with_sanitized_reason(deployment, monkeypatch):
    install, state, record = activate(deployment)
    monkeypatch.setattr(watchdog.time, 'monotonic', lambda: 221.0)
    assert watchdog.recover(install) == 'rolled_back'
    assert (install / 'current').resolve() == Path(record['previous'])
    assert watchdog.inspect_update(install)['last_error'] == 'confirmation_timeout'
    assert deployment[3][0] == ['systemctl', 'restart', 'ems-device.service']
    assert (state / 'agent.sqlite').read_bytes() == b'identity credentials outbox'


def test_wall_clock_changes_do_not_extend_confirmation_deadline(deployment, monkeypatch):
    install, _, _ = activate(deployment)
    monkeypatch.setattr(watchdog.time, 'time', lambda: -1000000)
    monkeypatch.setattr(watchdog.time, 'monotonic', lambda: 221.0)
    assert watchdog.recover(install) == 'rolled_back'


def test_late_confirmation_after_deadline_is_rejected(deployment, monkeypatch):
    install, state, record = activate(deployment)
    watchdog.atomic_json(state / watchdog.HEALTH, evidence(record, observed_uptime=221))
    monkeypatch.setattr(watchdog.time, 'monotonic', lambda: 221.0)
    assert watchdog.recover(install) == 'rolled_back'


def test_reboot_before_confirmation_rolls_back_without_trusting_old_uptime(deployment, monkeypatch):
    install, state, record = activate(deployment)
    watchdog.atomic_json(state / watchdog.HEALTH, evidence(record))
    monkeypatch.setattr(watchdog, 'kernel_boot_id', lambda: 'kernel-2')
    monkeypatch.setattr(watchdog.time, 'monotonic', lambda: 30.0)
    assert watchdog.recover(install) == 'rolled_back'
    assert watchdog.inspect_update(install)['last_error'] == 'reboot_before_confirmation'


@pytest.mark.parametrize('point', ['before_switch', 'after_switch', 'during_rollback'])
def test_process_death_at_activation_boundaries_is_recoverable(deployment, monkeypatch, point):
    install, _, record, _ = deployment
    script = '''
import os, sys
from pathlib import Path
from ems_device import update_watchdog as w
root, point = Path(sys.argv[1]), sys.argv[2]
record = w.read_json(root / w.JOURNAL)
if point != 'before_switch':
    w.replace_link(root / 'current', Path(record['release']))
if point == 'during_rollback':
    record.update(phase='rolling_back', last_error='activation_failed')
    w.atomic_json(root / w.JOURNAL, record)
os._exit(91)
'''
    result = subprocess.run([sys.executable, '-c', script, str(install), point], timeout=10)
    assert result.returncode == 91
    monkeypatch.setattr(watchdog.time, 'monotonic', lambda: 221.0)
    assert watchdog.recover(install) == 'rolled_back'
    assert (install / 'current').resolve() == Path(record['previous'])


def test_failed_rollback_is_reported_without_exception_body(deployment, monkeypatch):
    install, _, _ = activate(deployment)
    monkeypatch.setattr(watchdog.time, 'monotonic', lambda: 221.0)
    def fail(command):
        raise RuntimeError('secret exception body')
    monkeypatch.setattr(watchdog, '_run', fail)
    assert watchdog.recover(install) == 'rollback_failed'
    saved = (install / watchdog.JOURNAL).read_text()
    assert 'secret exception body' not in saved
    assert watchdog.inspect_update(install)['last_error'] == 'rollback_start_failed'


def test_watchdog_does_not_race_an_active_installer(deployment):
    install, _, _ = activate(deployment)
    with watchdog.deployment_lock(install):
        assert watchdog.recover(install) == 'busy'
    assert not deployment[3]


def test_status_symlink_is_not_followed_by_root_helper(deployment):
    install, state, record = activate(deployment)
    external = state / 'external'
    watchdog.atomic_json(external, evidence(record))
    (state / watchdog.HEALTH).unlink()
    (state / watchdog.HEALTH).symlink_to(external)
    assert watchdog.recover(install) == 'awaiting_confirmation'


def test_health_evidence_is_bounded_and_regular(tmp_path):
    oversized = tmp_path / 'huge'
    oversized.write_bytes(b' ' * 65537)
    with pytest.raises(ValueError, match='too_large'):
        watchdog.read_json(oversized)
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match='invalid_status_file'):
        watchdog.read_json(fifo)


def test_atomic_journal_failure_preserves_last_committed_record(tmp_path, monkeypatch):
    path = tmp_path / 'journal.json'
    watchdog.atomic_json(path, {'phase': 'switching'})
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(watchdog.os, 'replace', fail)
    with pytest.raises(OSError):
        watchdog.atomic_json(path, {'phase': 'awaiting_confirmation'})
    assert watchdog.read_json(path) == {'phase': 'switching'}
    assert list(tmp_path.iterdir()) == [path]


def test_runtime_contact_contains_matching_attempt_and_current_pid(deployment, monkeypatch):
    install, state, record = activate(deployment)
    monkeypatch.setattr(runtime_health, 'PROCESS_RELEASE', record['release'])
    monkeypatch.setattr(runtime_health, '__version__', record['version'])
    runtime_health.record_contact(state, 'new-process', install_dir=install)
    saved = watchdog.read_json(state / watchdog.HEALTH)
    assert saved['attempt_id'] == record['attempt_id']
    assert saved['pid'] == os.getpid()
    assert (state / watchdog.HEALTH).stat().st_mode & 0o777 == 0o600


def test_watchdog_script_runs_without_the_application_environment():
    result = subprocess.run([sys.executable, '-I', watchdog.__file__, '--help'],
                            check=True, capture_output=True, text=True)
    assert 'Recover or confirm' in result.stdout
