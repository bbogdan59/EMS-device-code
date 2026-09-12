#!/bin/sh
# Idempotent Raspberry Pi OS installer / factory provisioner.
set -eu

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

if [ -z "$PLATFORM_URL" ] && [ ! -f "$CONFIG_FILE" ]; then
    echo "Set EMS_PLATFORM_URL once, for example:" >&2
    echo "  sudo EMS_PLATFORM_URL=https://ems.example.com ./run.sh" >&2
    exit 2
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git python3 python3-venv

if ! id ems-device >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$STATE_DIR" --create-home ems-device
fi
usermod -aG dialout ems-device
install -d -m 0755 "$INSTALL_DIR" "$CONFIG_DIR"
install -d -m 0700 -o ems-device -g ems-device "$STATE_DIR"

# Copy only runtime sources. Identity is created in STATE_DIR and is never
# part of this checkout, so cloning/flashing the OS cannot clone credentials.
rm -rf "$INSTALL_DIR/src"
cp -a "$SCRIPT_DIR/src" "$INSTALL_DIR/src"
install -m 0644 "$SCRIPT_DIR/pyproject.toml" "$INSTALL_DIR/pyproject.toml"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --disable-pip-version-check "$INSTALL_DIR"

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

install -m 0644 "$SCRIPT_DIR/deploy/ems-device.service" /etc/systemd/system/ems-device.service
systemctl daemon-reload

echo
echo "=== PRINT THIS LABEL AND KEEP THE DEVICE CODE SEALED ==="
runuser -u ems-device -- "$INSTALL_DIR/.venv/bin/ems-device" --config "$CONFIG_FILE" provision
echo "========================================================="
echo

systemctl enable --now ems-device
echo "Provisioning complete. The customer only needs power/network, RS485, and the sealed device code."
