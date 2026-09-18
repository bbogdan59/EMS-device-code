#!/bin/sh
# Exercises deploy/update.sh's smoke-test/rollback logic against a REAL venv
# and REAL pip install (fast, hermetic: no network beyond installing this
# repo's own already-vendored dependencies, no root, no systemd, no apt/
# useradd -- those stay in run.sh and are genuinely untestable here). Not a
# pytest test on purpose: it drives a shell script with subshells, not
# Python.
set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

# --- fixture: a GOOD source tree (the real repo) and a BROKEN one (real repo
# with a syntax error injected into cli.py) -----------------------------
GOOD_SCRIPT_DIR="$WORK/good"
BROKEN_SCRIPT_DIR="$WORK/broken"
mkdir -p "$GOOD_SCRIPT_DIR" "$BROKEN_SCRIPT_DIR"
cp -a "$REPO_ROOT/src" "$GOOD_SCRIPT_DIR/src"
cp -a "$REPO_ROOT/pyproject.toml" "$GOOD_SCRIPT_DIR/pyproject.toml"
cp -a "$REPO_ROOT/src" "$BROKEN_SCRIPT_DIR/src"
cp -a "$REPO_ROOT/pyproject.toml" "$BROKEN_SCRIPT_DIR/pyproject.toml"
printf 'this is not valid python (\n' >>"$BROKEN_SCRIPT_DIR/src/ems_device/cli.py"

run_update() {
    # $1 = SCRIPT_DIR to install from, $2 = INSTALL_DIR. Runs in a subshell
    # so the script's own `exit` on smoke-test failure doesn't kill this
    # test runner; echoes "<exit_code> <UPGRADE>" on stdout.
    (
        SCRIPT_DIR=$1
        INSTALL_DIR=$2
        export SCRIPT_DIR INSTALL_DIR
        set +e
        # `set -e` inside this nested subshell mirrors production (run.sh has
        # it too when it sources update.sh); the OUTER subshell stays under
        # `+e` purely so capturing ITS exit code below can't itself abort.
        (set -e; . "$REPO_ROOT/deploy/update.sh") >"$WORK/last_stdout" 2>"$WORK/last_stderr"
        code=$?
        set -e
        echo "$code"
    )
}

# 1. Fresh install, good source: succeeds.
INSTALL_1="$WORK/install1"
mkdir -p "$INSTALL_1"
code=$(run_update "$GOOD_SCRIPT_DIR" "$INSTALL_1")
[ "$code" = "0" ] || fail "fresh good install exited $code, expected 0: $(cat "$WORK/last_stderr")"
"$INSTALL_1/.venv/bin/python" -c "import ems_device.cli" || fail "fresh good install: import smoke test itself doesn't pass"
echo "ok: fresh install with good source succeeds"

# 2. Fresh install, broken source: fails cleanly, no rollback attempted
#    (nothing to roll back to -- this is a plain failed install).
INSTALL_2="$WORK/install2"
mkdir -p "$INSTALL_2"
code=$(run_update "$BROKEN_SCRIPT_DIR" "$INSTALL_2")
[ "$code" = "3" ] || fail "fresh broken install exited $code, expected 3"
grep -q "Fresh install failed" "$WORK/last_stderr" || fail "fresh broken install: missing expected stderr message"
echo "ok: fresh install with broken source fails with exit 3 and no rollback claim"

# 3. Upgrade good -> good: succeeds, UPGRADE path taken (.previous backups exist
#    -- cleanup only happens in run.sh's OWN post-restart systemd check, not here).
INSTALL_3="$WORK/install3"
mkdir -p "$INSTALL_3"
run_update "$GOOD_SCRIPT_DIR" "$INSTALL_3" >/dev/null
code=$(run_update "$GOOD_SCRIPT_DIR" "$INSTALL_3")
[ "$code" = "0" ] || fail "good->good upgrade exited $code, expected 0: $(cat "$WORK/last_stderr")"
[ -d "$INSTALL_3/src.previous" ] || fail "good->good upgrade: expected a .previous backup to exist (cleanup is run.sh's job)"
"$INSTALL_3/.venv/bin/python" -c "import ems_device.cli" || fail "good->good upgrade: new version doesn't import"
echo "ok: upgrade from good to good succeeds, leaves a backup for run.sh to clean up"

# 4. Upgrade good -> broken: rolls back automatically; the working version is
#    restored and importable again afterward.
INSTALL_4="$WORK/install4"
mkdir -p "$INSTALL_4"
run_update "$GOOD_SCRIPT_DIR" "$INSTALL_4" >/dev/null
code=$(run_update "$BROKEN_SCRIPT_DIR" "$INSTALL_4")
[ "$code" = "3" ] || fail "good->broken upgrade exited $code, expected 3"
grep -q "rolling back" "$WORK/last_stderr" || fail "good->broken upgrade: missing expected rollback stderr message"
"$INSTALL_4/.venv/bin/python" -c "import ems_device.cli" || fail "good->broken upgrade: rollback left the install broken"
grep -q "this is not valid python" "$INSTALL_4/src/ems_device/cli.py" && fail "good->broken upgrade: broken source is still installed after rollback"
echo "ok: upgrade from good to broken source rolls back automatically and stays importable"

echo "All deploy/update.sh rollback scenarios passed."
