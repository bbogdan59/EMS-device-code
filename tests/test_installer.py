from pathlib import Path


def test_reinstall_stops_lock_holder_and_has_failure_restart_trap():
    script = (Path(__file__).parents[1] / "run.sh").read_text()

    stop = script.index("systemctl stop ems-device")
    provision = script.index('runuser -u ems-device -- "$INSTALL_DIR/current/.venv/bin/ems-device"')
    start = script.index("systemctl enable --now ems-device")
    assert stop < provision < start
    assert "trap restart_existing_service EXIT INT TERM" in script
    assert "systemctl start ems-device || true" in script
