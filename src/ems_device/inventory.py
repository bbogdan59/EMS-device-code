"""Observed application inventory shared by enrollment, heartbeat and health."""

from importlib.resources import files
import platform
import re

from . import __version__


def snapshot(settings=None):
    settings = settings or {}
    try:
        build_id = files("ems_device").joinpath("build_id.txt").read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{40}(?:-dirty)?", build_id):
            build_id = None
    except OSError:
        build_id = None
    try:
        os_release = platform.freedesktop_os_release()
        os_version = " ".join(filter(None, (os_release.get("ID"), os_release.get("VERSION_ID")))) or None
    except OSError:
        os_version = None
    result = {
        "agent_version": __version__,
        "build_id": build_id,
        "hardware_platform": settings.get("hardware_platform"),
        "architecture": platform.machine() or None,
        "os_version": os_version,
    }
    return {key: value for key, value in result.items() if value is not None}
