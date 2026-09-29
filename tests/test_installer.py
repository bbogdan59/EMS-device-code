"""Execute the real installer shell with isolated OS/service commands."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys

import pytest


STUB = r'''#!PYTHON
import fcntl, json, os, shutil, signal, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ['INSTALL_TEST_ROOT'])
with (root / 'commands').open('a') as stream:
    stream.write(json.dumps([name, *args]) + '\n')
fail = os.environ.get('INSTALL_TEST_FAIL')
current = root / 'opt/current'
if name == 'id':
    if args == ['-u']: print(0)
elif name == 'flock':
    try: fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: sys.exit(1)
elif name == 'install':
    paths = []
    directory = '-d' in args
    i = 0
    while i < len(args):
        if args[i] in ('-m', '-o', '-g'): i += 2
        elif args[i] == '-d': i += 1
        else: paths.append(args[i]); i += 1
    if directory:
        for path in paths: Path(path).mkdir(parents=True, exist_ok=True)
    else: shutil.copyfile(*paths)
elif name == 'mv':
    os.replace(args[-2], args[-1])
elif name == 'python3':
    directory = Path(args[-1]) / 'bin'
    directory.mkdir(parents=True)
    for command in ('pip', 'ems-device'):
        (directory / command).symlink_to(root / 'bin/stub')
elif name == 'pip':
    assert current.resolve() == root / 'opt/releases/old'
    assert (current / 'original').read_text() == 'unchanged'
    assert (root / 'active').exists(), 'monitoring stopped during build'
    if fail == 'pip': sys.exit(9)
elif name == 'runuser':
    action = args[-1]
    if action == 'preflight':
        assert (root / 'active').exists(), 'preflight must run during monitoring'
    if action == 'provision':
        assert not (root / 'active').exists()
        with (root / 'state/agent.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if fail == action: sys.exit(9)
elif name == 'systemctl':
    action = args[0]
    if action == 'show': print('active' if (root / 'active').exists() else 'inactive')
    elif action == 'stop':
        os.kill(int((root / 'monitor.pid').read_text()), signal.SIGTERM)
        with (root / 'state/agent.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        (root / 'active').unlink(missing_ok=True)
        if fail == 'stop': sys.exit(9)
    elif action in ('start', 'restart'):
        if fail == 'restart' and current.resolve().name != 'old': sys.exit(9)
        (root / 'active').touch()
    elif action == 'is-active':
        if fail == 'health' and current.resolve().name != 'old': sys.exit(9)
        sys.exit(0 if (root / 'active').exists() else 3)
'''


@pytest.fixture
def installer(tmp_path):
    commands = tmp_path / 'bin'
    commands.mkdir()
    stub = commands / 'stub'
    stub.write_text(STUB.replace('PYTHON', sys.executable))
    stub.chmod(0o755)
    for name in ('id', 'apt-get', 'usermod', 'useradd', 'install', 'python3', 'runuser',
                 'systemctl', 'mv', 'chown', 'sleep', 'flock'):
        (commands / name).symlink_to(stub)
    source = tmp_path / 'checkout'
    (source / 'src/ems_device').mkdir(parents=True)
    (source / 'deploy').mkdir()
    (source / 'deploy/ems-device.service').write_text('service')
    for name in ('ems-device-update-watchdog.service', 'ems-device-update-watchdog.timer'):
        (source / 'deploy' / name).write_text('watchdog unit')
    (source / 'src/ems_device/update_watchdog.py').write_text('# standalone helper')
    (source / 'src/ems_device/__init__.py').write_text('# new code')
    (source / 'pyproject.toml').write_text('# project')
    script = source / 'run.sh'
    script.write_text((Path(__file__).parents[1] / 'run.sh').read_text())
    old = tmp_path / 'opt/releases/old'
    old.mkdir(parents=True)
    (old / 'original').write_text('unchanged')
    (tmp_path / 'opt/current').symlink_to(old)
    for name in ('config', 'state', 'systemd'):
        (tmp_path / name).mkdir()
    (tmp_path / 'config/config.toml').write_text('existing configuration')
    (tmp_path / 'state/identity').write_text('existing identity and outbox')
    monitor = subprocess.Popen([sys.executable, '-c',
        'import fcntl,sys,time; f=open(sys.argv[1], "a"); '
        'fcntl.flock(f, fcntl.LOCK_EX); print("ready", flush=True); time.sleep(120)',
        str(tmp_path / 'state/agent.lock')], stdout=subprocess.PIPE, text=True)
    assert monitor.stdout.readline().strip() == 'ready'
    (tmp_path / 'monitor.pid').write_text(str(monitor.pid))
    (tmp_path / 'active').touch()
    environment = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'],
                       INSTALL_TEST_ROOT=str(tmp_path), EMS_INSTALL_DIR=str(tmp_path / 'opt'),
                       EMS_CONFIG_DIR=str(tmp_path / 'config'), EMS_STATE_DIR=str(tmp_path / 'state'),
                       EMS_SYSTEMD_DIR=str(tmp_path / 'systemd'), EMS_HELPER_DIR=str(tmp_path / 'helper'))

    def run(failure=''):
        return subprocess.run(['sh', str(script)], env=dict(environment, INSTALL_TEST_FAIL=failure),
                              capture_output=True, text=True, timeout=20)
    yield run
    monitor.terminate()
    monitor.wait(timeout=5)
    monitor.stdout.close()


def test_update_while_monitoring_builds_separately_and_releases_agent_lock(installer, tmp_path):
    result = installer()
    assert result.returncode == 0, result.stderr
    old = tmp_path / 'opt/releases/old'
    current = (tmp_path / 'opt/current').resolve()
    assert current != old
    assert current.name.startswith('bootstrap.')
    assert (current / '.venv/bin/ems-device').exists()
    assert (tmp_path / 'opt/previous').resolve() == old
    assert (old / 'original').read_text() == 'unchanged'
    assert (tmp_path / 'active').exists()
    assert (tmp_path / 'config/config.toml').read_text() == 'existing configuration'
    assert (tmp_path / 'state/identity').read_text() == 'existing identity and outbox'


@pytest.mark.parametrize('failure', ['pip', 'preflight', 'stop', 'provision', 'restart', 'health'])
def test_failed_update_preserves_previous_release_and_restores_monitoring(installer, tmp_path, failure):
    result = installer(failure)
    assert result.returncode != 0
    old = tmp_path / 'opt/releases/old'
    assert (tmp_path / 'opt/current').resolve() == old
    assert (old / 'original').read_text() == 'unchanged'
    assert (tmp_path / 'active').exists()
    assert list((tmp_path / 'opt/releases').iterdir()) == [old]


def test_installer_refuses_concurrent_deployment_before_stopping_monitor(installer, tmp_path):
    with (tmp_path / 'opt/.update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = installer()
    assert result.returncode != 0
    assert 'already running' in result.stderr
    assert (tmp_path / 'active').exists()
    assert (tmp_path / 'opt/current').resolve().name == 'old'
