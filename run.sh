#!/bin/sh
# Idempotent Raspberry Pi OS installer / factory provisioner.
set -eu
umask 022

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo ./run.sh" >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALL_DIR=${EMS_INSTALL_DIR:-/opt/ems-device}
CONFIG_DIR=${EMS_CONFIG_DIR:-/etc/ems-device}
STATE_DIR=${EMS_STATE_DIR:-/var/lib/ems-device}
CONFIG_FILE="$CONFIG_DIR/config.toml"
PLATFORM_URL=${EMS_PLATFORM_URL:-}
SYSTEMD_DIR=${EMS_SYSTEMD_DIR:-/etc/systemd/system}
HELPER_DIR=${EMS_HELPER_DIR:-/usr/local/lib/ems-device}

if [ -z "$PLATFORM_URL" ] && [ ! -f "$CONFIG_FILE" ]; then
    echo "Set EMS_PLATFORM_URL once, for example:" >&2
    echo "  sudo EMS_PLATFORM_URL=https://ems.example.com ./run.sh" >&2
    exit 2
fi

install -d -m 0755 "$INSTALL_DIR"
# A separate deployment lock lets monitoring keep its own agent/RS485 lock
# throughout download/build. Signed updates use this same deployment lock.
exec 9>"$INSTALL_DIR/.update.lock"
if ! flock -n 9; then
    echo "Another installation/update is already running" >&2
    exit 1
fi
if [ -e "$INSTALL_DIR/update-state.json" ]; then
    python3 "$SCRIPT_DIR/src/ems_device/update_watchdog.py" --install-dir "$INSTALL_DIR" --check-idle
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git minisign python3 python3-venv

if ! id ems-device >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$STATE_DIR" --create-home ems-device
fi
usermod -aG dialout ems-device
install -d -m 0755 "$INSTALL_DIR" "$CONFIG_DIR"
install -d -m 0700 -o ems-device -g ems-device "$STATE_DIR"

# Bootstrap uses the same release/symlink layout as signed updates. Identity is
# kept separately in STATE_DIR and can never be included in a release bundle.
install -d -m 0755 "$INSTALL_DIR/releases"
PREVIOUS=""
if [ -L "$INSTALL_DIR/current" ]; then
    PREVIOUS=$(readlink -f "$INSTALL_DIR/current")
elif [ -e "$INSTALL_DIR/current" ]; then
    echo "current must be a release symlink" >&2
    exit 1
fi
BOOTSTRAP=$(mktemp -d "$INSTALL_DIR/releases/bootstrap.XXXXXXXX")
chmod 0755 "$BOOTSTRAP"
SERVICE_WAS_ACTIVE=0
SERVICE_STOP_ATTEMPTED=0
SWITCHED=0
COMPLETE=0
recover_install() {
    result=$?
    trap - EXIT INT TERM
    if [ "$COMPLETE" -eq 0 ]; then
        if [ "$SWITCHED" -eq 1 ]; then
            if [ -n "$PREVIOUS" ]; then
                ln -sfn "$PREVIOUS" "$INSTALL_DIR/.current.new"
                mv -Tf "$INSTALL_DIR/.current.new" "$INSTALL_DIR/current"
            else
                systemctl stop ems-device || true
                rm -f "$INSTALL_DIR/current"
            fi
        fi
        if [ "$SERVICE_STOP_ATTEMPTED" -eq 1 ] && [ "$SERVICE_WAS_ACTIVE" -eq 1 ]; then
            systemctl restart ems-device || echo "Recovery restart failed; inspect journalctl -u ems-device" >&2
        fi
        rm -rf "$BOOTSTRAP"
    fi
    exit "$result"
}
trap recover_install EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Never overwrite a running release or move a venv after creating it: its
# console scripts contain absolute interpreter paths.
cp -a "$SCRIPT_DIR/src" "$BOOTSTRAP/src"
if [ -e "$SCRIPT_DIR/.git" ]; then
    BUILD_ID=$(git -c safe.directory="$SCRIPT_DIR" -C "$SCRIPT_DIR" rev-parse --verify HEAD)
    if [ -n "$(git -c safe.directory="$SCRIPT_DIR" -C "$SCRIPT_DIR" status --porcelain --untracked-files=no)" ]; then
        BUILD_ID="$BUILD_ID-dirty"
    fi
    printf '%s\n' "$BUILD_ID" >"$BOOTSTRAP/src/ems_device/build_id.txt"
fi
install -m 0644 "$SCRIPT_DIR/pyproject.toml" "$BOOTSTRAP/pyproject.toml"
python3 -m venv "$BOOTSTRAP/.venv"
"$BOOTSTRAP/.venv/bin/pip" install --disable-pip-version-check "$BOOTSTRAP"

if [ ! -f "$CONFIG_FILE" ]; then
    cat >"$CONFIG_FILE" <<EOF
platform_url = "$PLATFORM_URL"
state_dir = "$STATE_DIR"
device_name = "EMS Raspberry Pi"
hardware_platform = "raspberry-pi-4"
reader = "disabled"
allow_simulated_upload = false
sample_seconds = 10

[modbus]
port = "/dev/serial/by-id/REPLACE_WITH_YOUR_ADAPTER"
profile = "$CONFIG_DIR/deye-verified.json"
device_id = 1
baudrate = 9600
parity = "N"
stopbits = 1
EOF
fi
chown root:ems-device "$CONFIG_FILE"
chmod 0640 "$CONFIG_FILE"

runuser -u ems-device -- "$BOOTSTRAP/.venv/bin/ems-device" --config "$CONFIG_FILE" preflight

install -m 0644 "$SCRIPT_DIR/deploy/ems-device.service" "$SYSTEMD_DIR/ems-device.service"
install -d -m 0755 "$HELPER_DIR"
install -m 0644 "$SCRIPT_DIR/src/ems_device/update_watchdog.py" "$HELPER_DIR/update_watchdog.py.new"
mv -f "$HELPER_DIR/update_watchdog.py.new" "$HELPER_DIR/update_watchdog.py"
install -m 0644 "$SCRIPT_DIR/deploy/ems-device-update-watchdog.service" "$SYSTEMD_DIR/ems-device-update-watchdog.service"
install -m 0644 "$SCRIPT_DIR/deploy/ems-device-update-watchdog.timer" "$SYSTEMD_DIR/ems-device-update-watchdog.timer"
systemctl daemon-reload
systemctl enable --now ems-device-update-watchdog.timer

# Stop even an inactive/auto-restarting service, so it cannot reacquire the
# agent lock between this check and provisioning. Register recovery first.
case "$(systemctl show -p ActiveState --value ems-device)" in
    active|activating|reloading) SERVICE_WAS_ACTIVE=1 ;;
esac
SERVICE_STOP_ATTEMPTED=1
systemctl stop ems-device

echo
echo "=== PRINT THIS LABEL AND KEEP THE DEVICE CODE SEALED ==="
runuser -u ems-device -- "$BOOTSTRAP/.venv/bin/ems-device" --config "$CONFIG_FILE" provision
echo "========================================================="
echo

if [ -n "$PREVIOUS" ]; then
    ln -sfn "$PREVIOUS" "$INSTALL_DIR/.previous.new"
    mv -Tf "$INSTALL_DIR/.previous.new" "$INSTALL_DIR/previous"
fi
SWITCHED=1
ln -sfn "$BOOTSTRAP" "$INSTALL_DIR/.current.new"
mv -Tf "$INSTALL_DIR/.current.new" "$INSTALL_DIR/current"
systemctl enable ems-device
systemctl restart ems-device
for attempt in 1 2 3 4 5; do
    sleep 1
    systemctl is-active --quiet ems-device
done
COMPLETE=1
trap - EXIT INT TERM
echo "Provisioning complete. The customer only needs power/network, RS485, and the sealed device code."
