"""Verified, atomic release installation with service rollback."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import urlparse

import httpx

MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_EXTRACT_BYTES = 256 * 1024 * 1024


@contextmanager
def update_lock(install_dir: Path):
    """Shared with run.sh; independent from the monitoring/serial-master lock."""
    install_dir.mkdir(parents=True, exist_ok=True)
    with (install_dir / ".update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another installation/update is already running") from None
        yield


def _download(url: str, destination: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("update URLs must use HTTPS")
    limit = MAX_DOWNLOAD_BYTES if destination.name == "release.tar.gz" else 64 * 1024
    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", url, headers={"User-Agent": "ems-device-updater/1"}) as response:
            response.raise_for_status()  # Reject redirects BEFORE contacting their target.
            if int(response.headers.get("Content-Length", 0)) > limit:
                raise ValueError("update download exceeds size limit")
            total = 0
            deadline = time.monotonic() + 300
            with destination.open("wb") as output:
                for chunk in response.iter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > limit or time.monotonic() > deadline:
                        raise ValueError("update download exceeds size/time limit")
                    output.write(chunk)


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        root = destination.resolve()
        members = []
        total = 0
        for member in bundle:
            total += member.size
            if len(members) >= 4096 or total > MAX_EXTRACT_BYTES:
                raise ValueError("archive exceeds extraction limit")
            target = (destination / member.name).resolve()
            if Path(member.name).is_absolute() or (root not in target.parents and target != root):
                raise ValueError("archive contains a path outside its release directory")
            if not (member.isfile() or member.isdir()):
                raise ValueError("archive must contain only regular files and directories")
            if member.mode & 0o7000 or any(part in {".venv", "agent.sqlite", "agent.sqlite-wal", "agent.sqlite-shm"}
                                         for part in Path(member.name).parts):
                raise ValueError("archive contains unsafe permissions or device state")
            member.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
            members.append(member)
        bundle.extractall(destination, members=members, filter="data")


def _replace_link(link: Path, target: Path) -> None:
    temporary = link.with_name(f".{link.name}.new")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def _check_service(service: str) -> None:
    # Type=simple can appear active before Python has even imported its modules.
    for _ in range(5):
        time.sleep(1)
        _run(["systemctl", "is-active", "--quiet", service])


def install_update(
    manifest_url: str,
    public_key: str,
    *,
    install_dir: Path = Path("/opt/ems-device"),
    config: Path = Path("/etc/ems-device/config.toml"),
    service: str = "ems-device.service",
) -> str:
    """Verify a signed manifest and artifact, activate it, or restore the prior release."""
    install_dir = install_dir.resolve()
    with update_lock(install_dir):
        return _install_update(manifest_url, public_key, install_dir, config, service)


def _install_update(manifest_url, public_key, install_dir, config, service):
    install_dir.mkdir(parents=True, exist_ok=True)
    releases = install_dir / "releases"
    releases.mkdir(mode=0o755, exist_ok=True)
    releases.chmod(0o755)
    current = install_dir / "current"
    if current.exists() and not current.is_symlink():
        raise ValueError("current must be a release symlink")
    previous = current.resolve(strict=True) if current.is_symlink() else None
    with tempfile.TemporaryDirectory(prefix="ems-update-") as temporary:
        workspace = Path(temporary)
        manifest_path = workspace / "manifest.json"
        signature_path = workspace / "manifest.json.minisig"
        _download(manifest_url, manifest_path)
        _download(f"{manifest_url}.minisig", signature_path)
        _run(["minisign", "-Vm", str(manifest_path), "-x", str(signature_path), "-P", public_key])
        manifest = json.loads(manifest_path.read_text())
        if set(manifest) != {"version", "url", "sha256"}:
            raise ValueError("manifest must contain only version, url and sha256")
        version = manifest["version"]
        if not isinstance(version, str) or not version or version.startswith(".") or len(version) > 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in version):
            raise ValueError("invalid release version")
        digest = manifest["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
            raise ValueError("invalid artifact digest")
        if not isinstance(manifest["url"], str):
            raise ValueError("invalid artifact URL")
        if shutil.disk_usage(releases).free < MAX_EXTRACT_BYTES * 2:
            raise ValueError("insufficient space for an update")
        archive = workspace / "release.tar.gz"
        _download(manifest["url"], archive)
        with archive.open("rb") as artifact:
            if hashlib.file_digest(artifact, "sha256").hexdigest() != digest.lower():
                raise ValueError("artifact digest mismatch")
        release = releases / version
        if release.exists() or release.is_symlink():
            raise ValueError("release already exists")
        # venv entry points embed absolute paths: build at the FINAL location.
        # Only current changes; the directory/venv must never be relocated.
        release.mkdir(mode=0o755)
        release.chmod(0o755)
        try:
            _safe_extract(archive, release)
            _run(["python3", "-m", "venv", str(release / ".venv")])
            _run([str(release / ".venv/bin/pip"), "install", "--disable-pip-version-check", str(release)])
            _run(["runuser", "-u", "ems-device", "--", str(release / ".venv/bin/ems-device"),
                  "--config", str(config), "preflight"])
        except BaseException:
            shutil.rmtree(release, ignore_errors=True)
            raise

    try:
        if previous is not None:
            _replace_link(install_dir / "previous", previous)
        _replace_link(current, release)
        _run(["systemctl", "restart", service])
        _check_service(service)
    except BaseException:
        if previous is not None:
            _replace_link(current, previous)
            _run(["systemctl", "restart", service])
            _check_service(service)
        else:
            _run(["systemctl", "stop", service])
            current.unlink(missing_ok=True)
        raise
    return version


def main() -> None:
    os.umask(0o022)  # Releases must be readable by the isolated service user.
    parser = argparse.ArgumentParser(description="Install a signed EMS device release")
    parser.add_argument("manifest_url")
    parser.add_argument("--public-key", required=True, help="trusted minisign public key")
    parser.add_argument("--install-dir", type=Path, default=Path("/opt/ems-device"))
    parser.add_argument("--config", type=Path, default=Path("/etc/ems-device/config.toml"))
    args = parser.parse_args()
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    print(install_update(args.manifest_url, args.public_key, install_dir=args.install_dir, config=args.config))
