# Sourced by run.sh (not executed standalone): copies runtime sources into
# INSTALL_DIR, installs them into .venv, smoke-tests the import, and rolls
# back to the previous copy on failure. Pure venv/pip/python operations only
# -- no apt/useradd/systemd -- so this half of the update logic can be
# exercised directly in tests/test_update_rollback.sh without root or a
# running init system; the OS-level provisioning steps that DO need those
# stay in run.sh itself.
#
# Requires SCRIPT_DIR and INSTALL_DIR to already be set by the caller.
# Leaves UPGRADE=0|1 and the _rollback_update function in the caller's
# shell (this file must be sourced with ".", never executed in a subshell,
# or both are lost the instant it returns).
#
# Verified update with rollback (issue #4): "verified" here means smoke
# -tested + rolled back automatically on failure, NOT a cryptographic
# signature -- this project has no established signing key/trust-anchor
# process yet, so claiming one would be inventing infrastructure nobody
# asked for or can audit (tracked separately, not silently pretended away).
# On every run that finds a PREVIOUS install, that install is backed up
# before being replaced; if the new one fails an import smoke test, the
# backup is restored and reinstalled so the unit keeps running the last
# version that actually worked instead of being bricked mid-update. A
# second gate -- does the systemd service stay active after a real restart
# with the new code -- lives in run.sh itself, since it needs systemd.

UPGRADE=0
if [ -d "$INSTALL_DIR/src" ]; then
    UPGRADE=1
    rm -rf "$INSTALL_DIR/src.previous"
    cp -a "$INSTALL_DIR/src" "$INSTALL_DIR/src.previous"
    cp -a "$INSTALL_DIR/pyproject.toml" "$INSTALL_DIR/pyproject.toml.previous"
fi

_rollback_update() {
    rm -rf "$INSTALL_DIR/src"
    mv "$INSTALL_DIR/src.previous" "$INSTALL_DIR/src"
    mv "$INSTALL_DIR/pyproject.toml.previous" "$INSTALL_DIR/pyproject.toml"
    rm -rf "$INSTALL_DIR/build"
    "$INSTALL_DIR/.venv/bin/pip" install --disable-pip-version-check --force-reinstall --no-cache-dir "$INSTALL_DIR" >/dev/null
}

rm -rf "$INSTALL_DIR/src"
cp -a "$SCRIPT_DIR/src" "$INSTALL_DIR/src"
install -m 0644 "$SCRIPT_DIR/pyproject.toml" "$INSTALL_DIR/pyproject.toml"
[ -d "$INSTALL_DIR/.venv" ] || python3 -m venv "$INSTALL_DIR/.venv"
# --force-reinstall: a local-path install with an unchanged version number
# (pyproject.toml's version doesn't bump on every commit) is otherwise
# silently treated by pip as "already satisfied" and NOT rebuilt from the
# current file content. rm -rf build/ + --no-cache-dir: setuptools leaves a
# build/lib/ cache INSIDE this directory; because `cp -a`/`mv` preserve
# mtimes, a rolled-back (or any mtime-older) source file can look "not
# newer" than that stale cache to distutils' incremental build, which then
# reuses the stale copy instead of rebuilding -- caught by
# tests/test_update_rollback.sh, which failed here even WITH
# --force-reinstall until both of these were added; a rollback's reinstall
# was silently keeping the broken version's code.
rm -rf "$INSTALL_DIR/build"
"$INSTALL_DIR/.venv/bin/pip" install --disable-pip-version-check --force-reinstall --no-cache-dir "$INSTALL_DIR"

if ! "$INSTALL_DIR/.venv/bin/python" -c "import ems_device.cli" 2>/dev/null; then
    if [ "$UPGRADE" -eq 1 ]; then
        echo "Update failed a basic import smoke test; rolling back to the previous version." >&2
        _rollback_update
        exit 3
    fi
    echo "Fresh install failed a basic import smoke test (ems_device.cli)." >&2
    exit 3
fi
