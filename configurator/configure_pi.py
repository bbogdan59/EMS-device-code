#!/usr/bin/env python3
"""Local operator tool: drives `run.sh` on a Raspberry Pi over SSH, so a
technician never has to manually `git clone`/SSH onto the unit themselves.

Runs on the TECHNICIAN's laptop, not on the Pi -- separate from the
`ems_device` package (which stays minimal/pinned for the on-device install;
this tool's own dependency, paramiko, never reaches the Pi). It does exactly
what a technician would do by hand: package this checkout, copy it to the
Pi, run `sudo ./run.sh` there, and relay the output (including the serial
and Device Code) back to this terminal. It does not reimplement any install
logic -- `run.sh`/`deploy/update.sh` remain the single source of truth, so
this tool automatically gets their smoke-test/rollback behavior for free.

Usage:
    pip install -r configurator/requirements.txt
    python3 configurator/configure_pi.py [--host HOST] [--user USER] [--platform-url URL]

All of --host/--user/--platform-url are optional; missing ones are prompted
for interactively. The SSH and sudo passwords are always prompted (never a
CLI flag, so they never land in shell history) unless EMS_PI_SSH_PASSWORD /
EMS_PI_SUDO_PASSWORD are set, which is meant for scripted/CI-style runs, not
interactive use -- see configurator/README.md for the security tradeoffs.
"""
from __future__ import annotations

import argparse
import fnmatch
import getpass
import io
import os
import posixpath
import socket
import sys
import tarfile
import time
from pathlib import Path

try:
    import paramiko
except ImportError:  # pragma: no cover - exercised via README instructions, not tests
    print("paramiko is required: pip install -r configurator/requirements.txt", file=sys.stderr)
    raise SystemExit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Never shipped to the Pi: local dev/build artifacts. `build/` specifically
# caused a real staleness bug fixed in deploy/update.sh (issue #4) -- never
# transfer one from the technician's own dev checkout either.
TAR_EXCLUDE_PATTERNS = (
    ".git", ".git/*",
    ".venv", ".venv/*", "venv", "venv/*",
    "__pycache__", "*/__pycache__", "*/__pycache__/*",
    "*.egg-info", "*/*.egg-info", "*/*.egg-info/*",
    "build", "build/*",
    ".pytest_cache", ".pytest_cache/*", "*/.pytest_cache", "*/.pytest_cache/*",
    "configurator/.venv", "configurator/.venv/*",
    "*.pyc",
)

DEFAULT_AUTODETECT_HOSTS = ("raspberrypi.local", "raspberrypi.lan")

# Where run.sh installs the agent (see README.md) -- used to detect a prior
# install and, if the operator asks for one, to invoke the already-existing
# `ems-device reset` CLI action (issue #3) remotely, over the same SSH
# session. No new device-side code: this only orchestrates what a technician
# would otherwise type by hand.
AGENT_BIN = "/opt/ems-device/.venv/bin/ems-device"
AGENT_CONFIG = "/etc/ems-device/config.toml"
AGENT_USER = "ems-device"

RESET_MODE_CHOICES = {
    "": "none", "n": "none", "none": "none",
    "s": "soft", "soft": "soft",
    "f": "factory", "factory": "factory",
}


def should_exclude(relative_path: str) -> bool:
    """`relative_path` uses forward slashes, no leading './'."""
    return any(fnmatch.fnmatch(relative_path, pattern) for pattern in TAR_EXCLUDE_PATTERNS)


def build_tarball(repo_root: Path) -> bytes:
    """Packages the checkout into an in-memory tar.gz, excluding local-only
    artifacts (see TAR_EXCLUDE_PATTERNS). Returns the raw bytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(repo_root.rglob("*")):
            relative = path.relative_to(repo_root).as_posix()
            if should_exclude(relative):
                continue
            if path.is_dir():
                continue
            tar.add(path, arcname=relative)
    return buffer.getvalue()


def autodetect_host(candidates=DEFAULT_AUTODETECT_HOSTS, timeout=1.5) -> str | None:
    """Tries the default Raspberry Pi OS mDNS hostname(s). Best-effort only
    -- returns None (never raises) so callers always fall back to prompting;
    this is not a network scanner, just a resolve of the well-known default
    hostname mDNS/avahi already advertises out of the box."""
    original_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        for host in candidates:
            try:
                return socket.gethostbyname(host)
            except OSError:
                continue
        return None
    finally:
        socket.setdefaulttimeout(original_timeout)


def parse_run_sh_output(output: str) -> dict:
    """Extracts the operator-facing summary run.sh prints (serial, Device
    Code, enrollment status) from its captured stdout. Returns an empty
    dict for any field not found -- never fabricates a value."""
    result = {}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Serial:"):
            result["serial"] = line.split(":", 1)[1].strip()
        elif line.startswith("Device code"):
            result["device_code"] = line.split(":", 1)[1].strip()
        elif line.startswith("Enrollment:"):
            result["enrollment"] = line.split(":", 1)[1].strip()
    return result


def remote_run_command(staging_dir: str, platform_url: str) -> str:
    env_prefix = f'EMS_PLATFORM_URL="{platform_url}" ' if platform_url else ""
    return f"sudo -S -p '' bash -c 'cd {posixpath.join(staging_dir, REPO_ROOT.name)} && {env_prefix}./run.sh'"


def remote_agent_command(action: str, extra_args: str = "") -> str:
    """Builds the same command a technician runs by hand (see README.md's
    diagnostic snippets) -- always as the isolated `ems-device` user, never
    root, matching how the systemd service itself runs the agent."""
    tail = f" {extra_args}" if extra_args else ""
    return f"sudo -S -p '' -u {AGENT_USER} {AGENT_BIN} --config {AGENT_CONFIG} {action}{tail}"


def remote_reset_command(mode: str, confirm_serial: str) -> str:
    """`mode` is 'soft' or 'factory' (never 'none' -- callers only invoke
    this once a reset was actually requested)."""
    factory_flag = " --factory" if mode == "factory" else ""
    return remote_agent_command("reset", f"--confirm-serial {confirm_serial}{factory_flag}")


def parse_identity_output(output: str) -> dict:
    """Extracts the `key=value` lines `ems-device ... identity` prints.
    Missing keys are simply absent -- never fabricated."""
    result = {}
    for line in output.splitlines():
        line = line.strip()
        key, sep, value = line.partition("=")
        if sep and key in ("serial_number", "installation_uuid", "enrollment_status"):
            result[key] = value
    return result


class ConfiguratorError(Exception):
    pass


def connect(host: str, port: int, username: str, password: str) -> "paramiko.SSHClient":
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, port=port, username=username, password=password, timeout=10)
    except (paramiko.AuthenticationException, socket.error, paramiko.SSHException) as exc:
        raise ConfiguratorError(f"Could not connect to {username}@{host}:{port}: {exc}") from exc
    transport = client.get_transport()
    if transport is not None:
        key = transport.get_remote_server_key()
        print(f"Connected. Host key fingerprint: {key.get_name()} {key.get_fingerprint().hex()}")
        print("Verify this against the Pi's own `ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub` if you want to rule out a network MITM.")
    return client


def upload_and_extract(client: "paramiko.SSHClient", tarball: bytes, staging_dir: str) -> None:
    _stdin, stdout, stderr = client.exec_command(f"mkdir -p {staging_dir}")
    if stdout.channel.recv_exit_status() != 0:
        raise ConfiguratorError(f"Could not create staging directory on the Pi: {stderr.read().decode(errors='replace')}")

    sftp = client.open_sftp()
    remote_tar_path = posixpath.join(staging_dir, "checkout.tar.gz")
    with sftp.open(remote_tar_path, "wb") as remote_file:
        remote_file.write(tarball)
    sftp.close()

    extract_dir = posixpath.join(staging_dir, REPO_ROOT.name)
    stdin, stdout, stderr = client.exec_command(
        f"rm -rf {extract_dir} && mkdir -p {extract_dir} && "
        f"tar xzf {remote_tar_path} -C {extract_dir} && rm -f {remote_tar_path}"
    )
    exit_status = stdout.channel.recv_exit_status()
    if exit_status != 0:
        raise ConfiguratorError(f"Failed to extract checkout on the Pi: {stderr.read().decode(errors='replace')}")


def run_remote_streaming(client: "paramiko.SSHClient", command: str, sudo_password: str) -> tuple[int, str]:
    """Runs `command` with a pty (stdout+stderr interleaved in real time,
    printed live) and feeds `sudo_password` to `sudo -S`'s stdin. Returns
    (exit_status, full_captured_output)."""
    stdin, stdout, _stderr = client.exec_command(command, get_pty=True)
    stdin.write(sudo_password + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()  # EOF: a wrong password fails fast instead of sudo hanging on a retry prompt

    channel = stdout.channel
    captured = []
    while True:
        if channel.recv_ready():
            chunk = channel.recv(4096).decode(errors="replace")
            sys.stdout.write(chunk)
            sys.stdout.flush()
            captured.append(chunk)
        elif channel.exit_status_ready():
            break
        else:
            time.sleep(0.05)
    while channel.recv_ready():
        chunk = channel.recv(4096).decode(errors="replace")
        sys.stdout.write(chunk)
        captured.append(chunk)
    return channel.recv_exit_status(), "".join(captured)


def check_existing_install(client: "paramiko.SSHClient") -> bool:
    """True if a prior run.sh already installed the agent binary on this Pi.
    A fresh, never-provisioned unit legitimately fails this check -- that's
    the normal first-install path, not an error."""
    _stdin, stdout, _stderr = client.exec_command(f"test -x {AGENT_BIN}")
    return stdout.channel.recv_exit_status() == 0


def fetch_remote_identity(client: "paramiko.SSHClient", sudo_password: str) -> dict:
    """Runs `ems-device ... identity` on the Pi and parses its output. Used
    only to learn the current serial so a reset can be offered/confirmed --
    never to print or store any secret (identity has none)."""
    command = remote_agent_command("identity")
    exit_status, output = run_remote_streaming(client, command, sudo_password)
    if exit_status != 0:
        raise ConfiguratorError(f"Could not read the existing device identity (exit {exit_status}).")
    return parse_identity_output(output)


def perform_reset(client: "paramiko.SSHClient", sudo_password: str, mode: str, confirm_serial: str) -> tuple[int, str]:
    """Invokes the existing `ems-device reset` CLI action (issue #3) remotely.
    `mode` is 'soft' (clear assignment, keep identity) or 'factory' (wipe
    everything, issue a brand new identity) -- reuses the device's own
    --confirm-serial safety check, nothing reimplemented here."""
    command = remote_reset_command(mode, confirm_serial)
    return run_remote_streaming(client, command, sudo_password)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="Pi hostname/IP. Autodetects raspberrypi.local if omitted.")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--user", help="SSH username on the Pi.")
    parser.add_argument("--platform-url", default=os.environ.get("EMS_PLATFORM_URL", ""),
                        help="Only needed on first install; existing config.toml is left alone on updates.")
    args = parser.parse_args(argv)

    host = args.host
    if not host:
        print("Looking for raspberrypi.local ...")
        detected = autodetect_host()
        if detected:
            print(f"Found {detected}.")
            typed = input(f"Use {detected}? [Y/n/other host]: ").strip()
            host = detected if typed.lower() in ("", "y", "yes") else (typed or detected)
        else:
            print("Could not autodetect a Pi on the network.")
            host = input("Pi hostname or IP: ").strip()
    if not host:
        print("No host given; nothing to do.", file=sys.stderr)
        return 2

    username = args.user or input("SSH username: ").strip()
    if not username:
        print("No username given; nothing to do.", file=sys.stderr)
        return 2

    password = os.environ.get("EMS_PI_SSH_PASSWORD") or getpass.getpass(f"SSH password for {username}@{host}: ")
    sudo_password = os.environ.get("EMS_PI_SUDO_PASSWORD") or getpass.getpass(
        "Sudo password on the Pi (press Enter to reuse the SSH password): "
    ) or password

    platform_url = args.platform_url
    if not platform_url:
        platform_url = input(
            "Platform URL (only needed on first install; leave empty to keep existing config.toml): "
        ).strip()

    try:
        client = connect(host, args.port, username, password)
    except ConfiguratorError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        if check_existing_install(client):
            print("\nThis Pi already has the agent installed.")
            try:
                identity = fetch_remote_identity(client, sudo_password)
            except ConfiguratorError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            serial = identity.get("serial_number")
            if serial:
                print(f"  Serial: {serial}")
                if identity.get("enrollment_status"):
                    print(f"  Enrollment: {identity['enrollment_status']}")
                answer = input(
                    "\nReset before reconfiguring?\n"
                    "  [n] none -- keep the current assignment/identity, just update (default)\n"
                    "  [s] soft -- clear the station assignment, issue a new Device Code, keep the serial\n"
                    "  [f] factory -- wipe everything (identity, outbox, dead-letter); a brand new serial is issued\n"
                    "Choice [n/s/f]: "
                ).strip().lower()
                mode = RESET_MODE_CHOICES.get(answer, "none")
                if answer and mode == "none" and answer not in RESET_MODE_CHOICES:
                    print(f"Unrecognized choice {answer!r}; defaulting to no reset.")
                if mode == "factory":
                    confirm = input(
                        f"This PERMANENTLY wipes device {serial} (identity, outbox, dead-letter) and issues a "
                        f"new serial. Type the serial ({serial}) again to confirm, or leave empty to cancel: "
                    ).strip()
                    if confirm != serial:
                        print("Factory reset not confirmed; continuing without a reset.")
                        mode = "none"
                if mode != "none":
                    print(f"\nRunning {mode} reset on the Pi ...\n")
                    exit_status, _reset_output = perform_reset(client, sudo_password, mode, serial)
                    if exit_status != 0:
                        print(f"\nReset exited with status {exit_status}; aborting before reconfiguring.", file=sys.stderr)
                        return exit_status
            else:
                print("Could not determine the existing serial; continuing without offering a reset.")

        print("\nPackaging this checkout ...")
        tarball = build_tarball(REPO_ROOT)
        staging_dir = f".ems-configurator/deploy-{int(time.time())}"
        print(f"Copying to the Pi ({len(tarball)} bytes) ...")
        upload_and_extract(client, tarball, staging_dir)

        print("Running run.sh on the Pi (this installs packages -- may take a few minutes) ...\n")
        command = remote_run_command(staging_dir, platform_url)
        exit_status, output = run_remote_streaming(client, command, sudo_password)

        if exit_status != 0:
            print(f"\nrun.sh exited with status {exit_status}. Staging directory left at "
                  f"~/{staging_dir} on the Pi for inspection.", file=sys.stderr)
            return exit_status

        _i, cleanup_out, _e = client.exec_command(f"rm -rf {staging_dir}")  # best-effort cleanup on success only
        cleanup_out.channel.recv_exit_status()  # wait for it so it isn't cut short by client.close() below

        summary = parse_run_sh_output(output)
        print("\n" + "=" * 60)
        if summary:
            print("Provisioning complete:")
            for key, label in (("serial", "Serial"), ("device_code", "Device code"), ("enrollment", "Enrollment")):
                if key in summary:
                    print(f"  {label}: {summary[key]}")
            print("\nKeep the Device Code sealed until customer setup -- do not photograph/publish it.")
        else:
            print("run.sh finished but no Serial/Device code line was found in its output; "
                  "check the transcript above.")
        print("=" * 60)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
