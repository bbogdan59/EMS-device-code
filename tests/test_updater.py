import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from ems_device import updater


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
