import io
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from configure_pi import (
    ConfiguratorError,
    build_tarball,
    parse_run_sh_output,
    remote_run_command,
    run_remote_streaming,
    should_exclude,
    upload_and_extract,
)


def test_should_exclude_local_only_artifacts():
    for path in (
        ".git", ".git/HEAD",
        ".venv", ".venv/bin/python",
        "src/ems_device/__pycache__", "src/ems_device/__pycache__/cli.cpython-311.pyc",
        "src/ems_device.egg-info", "src/ems_device.egg-info/PKG-INFO",
        "build", "build/lib/ems_device/cli.py",
        ".pytest_cache", ".pytest_cache/README.md",
        "configurator/.venv", "configurator/.venv/bin/python",
        "foo.pyc",
    ):
        assert should_exclude(path), f"expected {path!r} to be excluded"


def test_should_not_exclude_real_source_files():
    for path in (
        "run.sh", "README.md", "pyproject.toml",
        "src/ems_device/cli.py", "src/ems_device/__init__.py",
        "deploy/update.sh", "deploy/ems-device.service",
        "profiles/deye_sg04lp3_candidate.json",
        "configurator/configure_pi.py", "configurator/README.md",
        "tests/test_agent.py",
    ):
        assert not should_exclude(path), f"did not expect {path!r} to be excluded"


def test_build_tarball_excludes_git_and_venv_but_keeps_run_sh(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("#!/bin/sh")
    (repo / "src" / "ems_device").mkdir(parents=True)
    (repo / "src" / "ems_device" / "cli.py").write_text("# real source")
    (repo / "src" / "ems_device" / "__pycache__").mkdir()
    (repo / "src" / "ems_device" / "__pycache__" / "cli.cpython-311.pyc").write_bytes(b"\x00")
    (repo / "run.sh").write_text("#!/bin/sh\necho hi\n")

    archive_bytes = build_tarball(repo)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        names = set(tar.getnames())

    assert "run.sh" in names
    assert "src/ems_device/cli.py" in names
    assert not any(name.startswith(".git") for name in names)
    assert not any(name.startswith(".venv") for name in names)
    assert not any("__pycache__" in name for name in names)


def test_parse_run_sh_output_extracts_serial_and_device_code():
    output = (
        "Cloning into 'EMS-device-code'...\n"
        "=== PRINT THIS LABEL AND KEEP THE DEVICE CODE SEALED ===\n"
        "Serial: EMS-ABCD-1234-EFGH-5678\n"
        "Device code (keep sealed until customer setup): ACT-QWERT-YUIOP\n"
        "Enrollment: pending\n"
        "=========================================================\n"
        "Provisioning complete. The customer only needs power/network, RS485, and the sealed device code.\n"
    )
    parsed = parse_run_sh_output(output)
    assert parsed == {
        "serial": "EMS-ABCD-1234-EFGH-5678",
        "device_code": "ACT-QWERT-YUIOP",
        "enrollment": "pending",
    }


def test_parse_run_sh_output_missing_fields_are_absent_not_fabricated():
    assert parse_run_sh_output("apt-get update\nSome unrelated log line\n") == {}


def test_parse_run_sh_output_update_run_has_no_device_code():
    """A repeat run (`git pull && sudo ./run.sh` equivalent) on an already
    -assigned device prints Serial/Enrollment but omits the Device Code line
    entirely (see cli.py's `provision` action) -- must not fabricate one."""
    output = "Serial: EMS-ABCD-1234-EFGH-5678\nEnrollment: assigned\n"
    parsed = parse_run_sh_output(output)
    assert parsed == {"serial": "EMS-ABCD-1234-EFGH-5678", "enrollment": "assigned"}
    assert "device_code" not in parsed


def test_remote_run_command_includes_platform_url_only_when_given():
    with_url = remote_run_command(".ems-configurator/deploy-1", "https://ems.example.com")
    assert 'EMS_PLATFORM_URL="https://ems.example.com"' in with_url
    assert "./run.sh" in with_url

    without_url = remote_run_command(".ems-configurator/deploy-1", "")
    assert "EMS_PLATFORM_URL" not in without_url
    assert "./run.sh" in without_url


def test_remote_run_command_cds_into_extracted_repo_dir():
    command = remote_run_command(".ems-configurator/deploy-1", "")
    from configure_pi import REPO_ROOT

    assert f"cd .ems-configurator/deploy-1/{REPO_ROOT.name}" in command


# --- fake paramiko SSHClient, for the orchestration logic (no real network) -


class FakeChannel:
    def __init__(self, chunks, exit_status):
        self._chunks = list(chunks)
        self._exit_status = exit_status
        self.written = b""
        self.write_closed = False

    def recv_ready(self):
        return bool(self._chunks)

    def recv(self, _n):
        return self._chunks.pop(0)

    def exit_status_ready(self):
        return not self._chunks

    def recv_exit_status(self):
        return self._exit_status

    def shutdown_write(self):
        self.write_closed = True


class FakeChannelFile:
    """Stands in for the paramiko ChannelFile objects exec_command() returns."""

    def __init__(self, channel):
        self.channel = channel

    def write(self, data):
        self.channel.written += data.encode() if isinstance(data, str) else data

    def flush(self):
        pass

    def read(self):
        return b""


class FakeSFTPFile:
    def __init__(self, sink: dict, key: str):
        self._sink = sink
        self._key = key

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def write(self, data):
        self._sink[self._key] = self._sink.get(self._key, b"") + data


class FakeSFTP:
    def __init__(self):
        self.files = {}

    def open(self, path, _mode):
        return FakeSFTPFile(self.files, path)

    def close(self):
        pass


class FakeSSHClient:
    """`exec_command` results are consumed in the order they're queued;
    `open_sftp()` always returns the same FakeSFTP so uploaded bytes are
    inspectable afterward."""

    def __init__(self, command_results):
        self._queue = list(command_results)
        self.commands = []
        self.channels = []
        self.sftp = FakeSFTP()

    def exec_command(self, command, get_pty=False):
        self.commands.append(command)
        chunks, exit_status = self._queue.pop(0)
        channel = FakeChannel(chunks, exit_status)
        self.channels.append(channel)
        return FakeChannelFile(channel), FakeChannelFile(channel), FakeChannelFile(channel)

    def open_sftp(self):
        return self.sftp


def test_run_remote_streaming_captures_output_and_exit_status(capsys):
    client = FakeSSHClient([([b"Serial: EMS-1\n", b"Enrollment: pending\n"], 0)])
    exit_status, output = run_remote_streaming(client, "sudo -S ./run.sh", "hunter2")
    assert exit_status == 0
    assert output == "Serial: EMS-1\nEnrollment: pending\n"
    assert "Serial: EMS-1" in capsys.readouterr().out  # streamed live, not just returned


def test_run_remote_streaming_sends_password_and_signals_eof():
    client = FakeSSHClient([([b"ok\n"], 0)])
    run_remote_streaming(client, "sudo -S ./run.sh", "hunter2")
    channel = client.channels[0]
    assert channel.written == b"hunter2\n"
    assert channel.write_closed is True  # EOF signaled so a wrong password fails fast


def test_run_remote_streaming_nonzero_exit_status_is_returned_not_raised():
    client = FakeSSHClient([([b"boom\n"], 3)])
    exit_status, output = run_remote_streaming(client, "sudo -S ./run.sh", "hunter2")
    assert exit_status == 3
    assert output == "boom\n"


def test_upload_and_extract_sends_tarball_bytes_and_runs_extract_commands():
    client = FakeSSHClient([
        ([], 0),  # mkdir -p staging_dir
        ([], 0),  # rm -rf/mkdir -p/tar xzf/rm -f extract sequence
    ])
    upload_and_extract(client, b"fake-tar-bytes", "staging")
    assert client.sftp.files["staging/checkout.tar.gz"] == b"fake-tar-bytes"
    assert any("mkdir -p staging" in c for c in client.commands)
    assert any("tar xzf staging/checkout.tar.gz" in c for c in client.commands)


def test_upload_and_extract_raises_on_mkdir_failure():
    client = FakeSSHClient([([], 1)])  # mkdir -p fails
    with pytest.raises(ConfiguratorError, match="staging directory"):
        upload_and_extract(client, b"bytes", "staging")


def test_upload_and_extract_raises_on_extract_failure():
    client = FakeSSHClient([
        ([], 0),  # mkdir -p staging_dir succeeds
        ([], 1),  # tar extraction fails
    ])
    with pytest.raises(ConfiguratorError, match="extract"):
        upload_and_extract(client, b"bytes", "staging")
