# Configurator: install/update the agent on a Raspberry Pi over SSH

Runs on your laptop, not on the Pi. It does exactly what the manual
technician flow in the main `README.md` does -- package this checkout,
copy it to the Pi, run `sudo ./run.sh` there -- so you never have to
`git clone`/SSH onto the unit yourself. It does not reimplement any install
logic: `run.sh`/`deploy/update.sh` remain the single source of truth, so
this tool automatically gets their smoke-test/rollback behavior too.

## Use

```sh
cd EMS-device-code
python3 -m venv configurator/.venv
configurator/.venv/bin/pip install -r configurator/requirements.txt
configurator/.venv/bin/python configurator/configure_pi.py
```

You'll be prompted for:

- **Host** -- tries to autodetect `raspberrypi.local` (the Raspberry Pi OS
  default mDNS hostname) first; type an IP/hostname yourself if that fails
  or you changed the hostname.
- **SSH username/password** -- whatever you set up when flashing the SD
  card (Raspberry Pi Imager) or created afterward.
- **Sudo password** -- press Enter to reuse the SSH password (the common
  case: same user, same password).
- **Platform URL** -- only used on a first install (`EMS_PLATFORM_URL`);
  leave empty on a repeat run and the existing `config.toml` is left alone,
  same as `run.sh` itself.

All of these can also be passed as `--host`/`--user`/`--platform-url` flags
(see `--help`) if you want to script it; the two passwords are **always**
prompted interactively (never a CLI flag, so they never land in shell
history), unless you explicitly set `EMS_PI_SSH_PASSWORD`/
`EMS_PI_SUDO_PASSWORD` in the environment for a scripted/CI-style run --
avoid that on a shared/logged shell.

Output streams live (you'll see the same `apt-get`/`pip install`/systemd
output a technician sitting at the Pi would), and on success prints a
summary with the serial and Device Code pulled straight out of `run.sh`'s
own output -- nothing is invented if a line isn't found.

Re-running is safe: it's the same idempotent `run.sh`/`deploy/update.sh`
flow, so this doubles as your remote-update tool (`git pull` locally, then
re-run the configurator) and gets its automatic rollback-on-failure for
free.

## Re-running on a Pi that's already installed

If the configurator finds an existing install on the target Pi (i.e. this
isn't the first run), it prints the current serial/enrollment status and
offers a reset **before** reconfiguring, reusing the device's own `ems-device
reset` CLI action (issue #3) -- nothing new is added on the device side:

- **[n] none** (default) -- keep the current assignment and identity, just
  update the software. Use this for routine updates on a device that's
  already claimed by a customer.
- **[s] soft** -- clears the station assignment and issues a new Device
  Code, but keeps the same serial/identity. Use this to take a returned/
  unclaimed unit back to "ready to ship" without wiping its history.
- **[f] factory** -- wipes everything (identity, outbox, dead-letter) and
  issues a brand new serial and Device Code. Use this only when repurposing
  hardware for an unrelated customer. This is destructive and irreversible;
  the configurator asks you to re-type the exact serial to confirm, on top
  of the device's own `--confirm-serial` check.

A reset failure (wrong sudo password, device CLI error) aborts before
touching the install -- it never silently proceeds to reconfigure a device
whose reset didn't actually happen.

## What it does, step by step

1. Connects over SSH (`paramiko`, TOFU host-key acceptance -- prints the
   fingerprint so you can cross-check `ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub`
   on the Pi yourself if you're on a network you don't fully trust).
2. Packages this local checkout into an in-memory tar.gz, excluding
   `.git`, `.venv`, `__pycache__`, `*.egg-info`, `build/`, `.pytest_cache`
   (the `build/` exclusion matters: a stale one caused a real staleness bug
   fixed in `deploy/update.sh` for issue #4 -- never ship one from your own
   dev checkout either).
3. Uploads it via SFTP to `~/.ems-configurator/deploy-<timestamp>/` on the
   Pi and extracts it there.
4. Runs `sudo ./run.sh` in that extracted copy, over a pty, feeding the
   sudo password to `sudo -S` via the SSH channel's stdin (never embedded
   in the command line, so it never shows up in the Pi's own `ps aux`).
5. On success, removes the staging directory. On failure, leaves it in
   place and tells you the path, so you can SSH in yourself and inspect
   what happened.

## Security notes (read before using on a network you don't control)

- **Host key verification is trust-on-first-use** (`AutoAddPolicy`), the
  same posture as a first manual `ssh pi@raspberrypi.local`. On a network
  you don't fully trust, verify the printed fingerprint against the Pi's
  own `/etc/ssh/ssh_host_*_key.pub` before typing your password.
- **Passwords never touch disk or shell history** from this tool's side
  (prompted via `getpass`, held in memory only) -- but SSH password auth
  itself is inherently weaker than key-based auth. If you manage many
  units, consider `ssh-copy-id`-ing a key to each Pi once and switching
  this tool to key-based auth (not implemented here yet -- password auth
  matches how `run.sh`'s own manual flow already works, so this is not a
  regression, just an opportunity for a follow-up).
- **This tool is not part of the on-device agent's protocol or trust
  boundary** -- it's a convenience wrapper around the exact same `run.sh`
  a technician would run by hand. Nothing here changes what the agent
  does once installed.
- The Device Code printed at the end is the same high-entropy bearer
  secret `run.sh` always prints -- keep your terminal scrollback as
  private as you would the Pi's own console.

## Tests

```sh
configurator/.venv/bin/pip install -r configurator/requirements-test.txt
configurator/.venv/bin/python -m pytest configurator/tests/ -v
```

No real SSH/hardware in tests -- they cover the pure logic (tar exclusion
patterns, `run.sh` output parsing, remote command construction) that
doesn't need a live connection. Nothing here validates an actual SSH
session against a real Pi; that remains a manual verification step for
whoever uses this tool for the first time on real hardware.
