"""Verified, atomic release installation with service rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def _download(url: str, destination: Path) -> None:
    if urlparse(url).scheme != "https":
        raise ValueError("update URLs must use HTTPS")
    request = Request(url, headers={"User-Agent": "ems-device-updater/1"})
    with urlopen(request, timeout=60) as response, destination.open("wb") as output:
        if response.geturl() != url:
            raise ValueError("update downloads must not redirect")
        shutil.copyfileobj(response, output)


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        root = destination.resolve()
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if root not in target.parents and target != root:
                raise ValueError("archive contains a path outside its release directory")
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError("archive links and devices are not allowed")
        bundle.extractall(destination, filter="data")


def install_update(
    manifest_url: str,
    public_key: str,
    *,
    install_dir: Path = Path("/opt/ems-device"),
    config: Path = Path("/etc/ems-device/config.toml"),
    service: str = "ems-device.service",
) -> str:
    """Verify a signed manifest and artifact, activate it, or restore the prior release."""
    install_dir.mkdir(parents=True, exist_ok=True)
    releases = install_dir / "releases"
    releases.mkdir(mode=0o755, exist_ok=True)
    current = install_dir / "current"
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
        if not isinstance(version, str) or not version or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in version):
            raise ValueError("invalid release version")
        digest = manifest["sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("invalid artifact digest")
        archive = workspace / "release.tar.gz"
        _download(manifest["url"], archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest.lower():
            raise ValueError("artifact digest mismatch")
        release = releases / version
        if release.exists():
            raise ValueError("release already exists")
        staging = releases / f".{version}.staging"
        staging.mkdir(mode=0o755)
        try:
            _safe_extract(archive, staging)
            _run(["python3", "-m", "venv", str(staging / ".venv")])
            _run([str(staging / ".venv/bin/pip"), "install", "--disable-pip-version-check", str(staging)])
            _run([str(staging / ".venv/bin/ems-device"), "--config", str(config), "health"])
            staging.rename(release)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    previous = current.resolve() if current.is_symlink() else None
    replacement = install_dir / ".current.new"
    replacement.unlink(missing_ok=True)
    replacement.symlink_to(release)
    os.replace(replacement, current)
    try:
        _run(["systemctl", "restart", service])
        _run(["systemctl", "is-active", "--quiet", service])
    except Exception:
        if previous is not None:
            replacement.symlink_to(previous)
            os.replace(replacement, current)
            _run(["systemctl", "restart", service])
        raise
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description="Install a signed EMS device release")
    parser.add_argument("manifest_url")
    parser.add_argument("--public-key", required=True, help="trusted minisign public key")
    parser.add_argument("--install-dir", type=Path, default=Path("/opt/ems-device"))
    parser.add_argument("--config", type=Path, default=Path("/etc/ems-device/config.toml"))
    args = parser.parse_args()
    print(install_update(args.manifest_url, args.public_key, install_dir=args.install_dir, config=args.config))
