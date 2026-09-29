import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from ems_device import updater


@pytest.fixture(autouse=True)
def no_service_wait(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(updater, "_state_directory", lambda config: tmp_path / "state")


def _bundle(path: Path, unsafe=False):
    with tarfile.open(path, "w:gz") as archive:
        data = b"[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n"
        info = tarfile.TarInfo("../escape" if unsafe else "pyproject.toml")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))


def test_safe_extract_rejects_traversal(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    _bundle(archive, unsafe=True)
    with pytest.raises(ValueError, match="outside"):
        updater._safe_extract(archive, tmp_path / "release")


def test_verified_update_activates_release(monkeypatch, tmp_path):
    archive = tmp_path / "source.tar.gz"
    _bundle(archive)
    manifest = {"version": "0.2.0", "url": "https://updates.example/release.tar.gz",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    downloads = {
        "https://updates.example/manifest.json": json.dumps(manifest).encode(),
        "https://updates.example/manifest.json.minisig": b"signature",
        manifest["url"]: archive.read_bytes(),
    }
    monkeypatch.setattr(updater, "_download", lambda url, destination: destination.write_bytes(downloads[url]))
    commands = []
    monkeypatch.setattr(updater, "_run", lambda command: commands.append(command))
    install = tmp_path / "opt"
    old = install / "releases/old"
    old.mkdir(parents=True)
    (install / "current").symlink_to(old)
    version = updater.install_update("https://updates.example/manifest.json", "RWtrusted", install_dir=install,
                                     config=tmp_path / "config.toml")
    assert version == "0.2.0"
    assert (install / "current").resolve() == install / "releases/0.2.0"
    assert commands[0][0:2] == ["minisign", "-Vm"]
    assert commands[-1] == ["systemctl", "is-active", "--quiet", "ems-device.service"]


def test_failed_health_check_rolls_back(monkeypatch, tmp_path):
    archive = tmp_path / "source.tar.gz"
    _bundle(archive)
    manifest = {"version": "0.2.0", "url": "https://updates.example/release.tar.gz",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    downloads = {"https://u/m": json.dumps(manifest).encode(), "https://u/m.minisig": b"sig",
                 manifest["url"]: archive.read_bytes()}
    monkeypatch.setattr(updater, "_download", lambda url, destination: destination.write_bytes(downloads[url]))
    old = tmp_path / "opt/releases/old"
    old.mkdir(parents=True)
    (tmp_path / "opt/current").symlink_to(old)
    def run(command):
        if command[:3] == ["systemctl", "is-active", "--quiet"]:
            raise RuntimeError("failed")
    monkeypatch.setattr(updater, "_run", run)
    with pytest.raises(RuntimeError):
        updater.install_update("https://u/m", "RWtrusted", install_dir=tmp_path / "opt",
                               config=tmp_path / "config.toml")
    assert (tmp_path / "opt/current").resolve() == old


@pytest.fixture
def release_download(monkeypatch, tmp_path):
    archive = tmp_path / 'source.tar.gz'
    _bundle(archive)
    manifest = {'version': '0.2.0', 'url': 'https://updates.example/release.tar.gz',
                'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    def download(url, destination):
        data = (archive.read_bytes() if destination.name == 'release.tar.gz' else
                b'sig' if destination.name.endswith('minisig') else json.dumps(manifest).encode())
        destination.write_bytes(data)
    monkeypatch.setattr(updater, '_download', download)
    old = tmp_path / 'opt/releases/old'
    old.mkdir(parents=True)
    (old / 'sentinel').write_text('original')
    (tmp_path / 'opt/current').symlink_to(old)
    return manifest


def test_update_console_script_remains_executable_after_activation(monkeypatch, tmp_path, release_download):
    import subprocess
    import sys
    def run(command):
        if command[:3] == ['python3', '-m', 'venv']:
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', command[-1]], check=True)
        elif command[0].endswith('/bin/pip'):
            release = Path(command[-1])
            executable = release / '.venv/bin/ems-device'
            executable.write_text(f'#!{release}/.venv/bin/python\nprint("preflight-ok")\n')
            executable.chmod(0o755)
        elif command[0] == 'runuser':
            subprocess.run(command[4:], check=True, capture_output=True)
    monkeypatch.setattr(updater, '_run', run)
    install = tmp_path / 'opt'
    updater.install_update('https://u/m', 'key', install_dir=install)
    output = subprocess.check_output([str(install / 'current/.venv/bin/ems-device')], text=True)
    assert output.strip() == 'preflight-ok'
    assert (install / 'previous').resolve() == install / 'releases/old'


def test_concurrent_update_does_not_download_or_touch_current(monkeypatch, tmp_path):
    import fcntl
    install = tmp_path / 'opt'
    install.mkdir()
    monkeypatch.setattr(updater, '_download', lambda *args: pytest.fail('download under concurrent deployment'))
    with (install / '.update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='already running'):
            updater.install_update('https://u/m', 'key', install_dir=install)


@pytest.mark.parametrize('failure', ['signature', 'install', 'preflight', 'restart', 'crash_loop'])
def test_failed_updates_preserve_release_and_restart_old_process(monkeypatch, tmp_path, release_download, failure):
    install = tmp_path / 'opt'
    commands = []
    active_checks = 0
    def run(command):
        nonlocal active_checks
        commands.append(command)
        if command[0] == 'minisign' and failure == 'signature':
            raise RuntimeError('signature')
        if command[0].endswith('/bin/pip') and failure == 'install':
            raise OSError('disk full')
        if command[-1] == 'preflight' and failure == 'preflight':
            raise RuntimeError('preflight')
        if (install / 'current').resolve().name == '0.2.0':
            if command[:2] == ['systemctl', 'restart'] and failure == 'restart':
                raise RuntimeError('restart')
            if command[:2] == ['systemctl', 'is-active']:
                active_checks += 1
                if active_checks == 2 and failure == 'crash_loop':
                    raise RuntimeError('process crashed after briefly appearing active')
    monkeypatch.setattr(updater, '_run', run)
    with pytest.raises((OSError, RuntimeError)):
        updater.install_update('https://u/m', 'key', install_dir=install)
    assert (install / 'current').resolve() == install / 'releases/old'
    assert (install / 'current/sentinel').read_text() == 'original'
    if failure in {'restart', 'crash_loop'}:
        assert commands.count(['systemctl', 'restart', 'ems-device.service']) == 2
    else:
        assert not any(command[0] == 'systemctl' for command in commands)
        assert not (install / 'releases/0.2.0').exists()


@pytest.mark.parametrize('version', ['.', '..', '../other', '/tmp/x', '.hidden', 'a' * 129])
def test_rejects_unsafe_version_before_install(monkeypatch, tmp_path, release_download, version):
    release_download['version'] = version
    monkeypatch.setattr(updater, '_run', lambda command: None)
    with pytest.raises(ValueError, match='version'):
        updater.install_update('https://u/m', 'key', install_dir=tmp_path / 'opt')
    assert (tmp_path / 'opt/current').resolve().name == 'old'


def test_hash_mismatch_does_not_touch_active_release(monkeypatch, tmp_path, release_download):
    release_download['sha256'] = '0' * 64
    monkeypatch.setattr(updater, '_run', lambda command: None)
    with pytest.raises(ValueError, match='digest mismatch'):
        updater.install_update('https://u/m', 'key', install_dir=tmp_path / 'opt')
    assert (tmp_path / 'opt/current').resolve().name == 'old'


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'setuid', 'state', 'venv', 'oversize'])
def test_rejects_unsafe_archive_contents(tmp_path, kind, monkeypatch):
    archive = tmp_path / 'unsafe.tar.gz'
    info = tarfile.TarInfo('file')
    if kind == 'symlink': info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
    if kind == 'hardlink': info.type, info.linkname = tarfile.LNKTYPE, '/etc/passwd'
    if kind == 'fifo': info.type = tarfile.FIFOTYPE
    if kind == 'setuid': info.mode = 0o4755
    if kind == 'state': info.name = 'state/agent.sqlite'
    if kind == 'venv': info.name = '.venv/bin/python'
    if kind == 'oversize': monkeypatch.setattr(updater, 'MAX_EXTRACT_BYTES', -1)
    with tarfile.open(archive, 'w:gz') as bundle:
        bundle.addfile(info)
    with pytest.raises(ValueError):
        updater._safe_extract(archive, tmp_path / 'release')


def test_insufficient_space_does_not_download_artifact(monkeypatch, tmp_path, release_download):
    from types import SimpleNamespace
    monkeypatch.setattr(updater.shutil, 'disk_usage', lambda path: SimpleNamespace(free=0))
    monkeypatch.setattr(updater, '_run', lambda command: None)
    with pytest.raises(ValueError, match='space'):
        updater.install_update('https://u/m', 'key', install_dir=tmp_path / 'opt')
    assert (tmp_path / 'opt/current').resolve().name == 'old'


def test_download_rejects_redirect_before_target_request(monkeypatch, tmp_path):
    import httpx
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={'Location': 'https://other.example/secret'})
    client_class = httpx.Client
    def client(**kwargs):
        assert kwargs['trust_env'] is False
        assert kwargs['follow_redirects'] is False
        return client_class(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(updater.httpx, 'Client', client)
    with pytest.raises(httpx.HTTPStatusError):
        updater._download('https://updates.example/manifest', tmp_path / 'manifest.json')
    assert len(requests) == 1
    assert not (tmp_path / 'manifest.json').exists()


@pytest.mark.parametrize('advertise_size', [True, False])
def test_download_is_bounded_with_or_without_content_length(monkeypatch, tmp_path, advertise_size):
    import httpx
    client_class = httpx.Client
    def handler(request):
        response = httpx.Response(200, content=b'x' * (64 * 1024 + 1))
        if not advertise_size:
            response.headers.pop('Content-Length', None)
        return response
    monkeypatch.setattr(updater.httpx, 'Client', lambda **kw: client_class(**kw, transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match='size'):
        updater._download('https://updates.example/manifest', tmp_path / 'manifest.json')


def test_activation_is_pending_until_new_process_confirms(monkeypatch, tmp_path, release_download):
    monkeypatch.setattr(updater, '_run', lambda command: None)
    install = tmp_path / 'opt'
    updater.install_update('https://u/m', 'key', install_dir=install)
    saved = updater.watchdog.read_json(install / updater.watchdog.JOURNAL)
    assert saved['phase'] == 'awaiting_confirmation'
    assert saved['previous'] == str(install / 'releases/old')
    with pytest.raises(ValueError, match='confirmation or recovery'):
        updater.install_update('https://u/m', 'key', install_dir=install)
