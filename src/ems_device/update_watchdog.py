"""Standalone, stdlib-only recovery helper, installed outside active releases.

The root-owned journal is the authority. Service-user health is only evidence
for a matching attempt, release and new process after authenticated contact.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import uuid

INSTALL_DIR = Path('/opt/ems-device')
JOURNAL = 'update-state.json'
HEALTH = 'runtime-health.json'
CONFIRM_SECONDS = 120


def kernel_boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return None


def read_json(path):
    # Never follow a service-user supplied symlink from the privileged helper.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('invalid_status_file')
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('status_file_too_large')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('invalid_status_object')
    return value


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value, *, mode=0o644):
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def replace_link(link, target):
    temporary = link.with_name(f'.{link.name}.new')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)
    sync_directory(link.parent)


@contextmanager
def deployment_lock(install_dir):
    with (install_dir / '.update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def begin(install_dir, release, previous, version, state_dir):
    """Must be committed before switching current; caller holds deployment lock."""
    try:
        old_boot = read_json(state_dir / HEALTH).get('boot_id')
    except (OSError, ValueError):
        old_boot = None
    record = {
        'schema': 1, 'attempt_id': str(uuid.uuid4()), 'phase': 'switching',
        'version': version, 'release': str(release), 'previous': str(previous) if previous else None,
        'state_dir': str(state_dir.resolve()), 'boot_id_before': old_boot,
        'kernel_boot_id': kernel_boot_id(), 'started_uptime': time.monotonic(),
        'deadline_uptime': time.monotonic() + CONFIRM_SECONDS, 'last_error': None,
    }
    atomic_json(install_dir / JOURNAL, record)
    return record


def _run(command):
    subprocess.run(command, check=True, timeout=45)


def _service_pid():
    result = subprocess.run(['systemctl', 'show', '-p', 'MainPID', '--value', 'ems-device.service'],
                            check=True, timeout=10, capture_output=True, text=True)
    return int(result.stdout.strip())


def _release(install_dir, value):
    path = Path(value)
    # Only root-installed, direct children of releases are valid targets.
    if path.parent != install_dir / 'releases' or path.is_symlink() or not path.is_dir():
        raise ValueError('invalid_release_target')
    return path


def _confirmed(record, health, now, boot):
    observed = health.get('observed_uptime')
    return (
        health.get('attempt_id') == record['attempt_id']
        and health.get('release') == record['release']
        and health.get('version') == record['version']
        and isinstance(health.get('boot_id'), str) and bool(health['boot_id'])
        and health['boot_id'] != record.get('boot_id_before')
        and health.get('kernel_boot_id') == boot == record.get('kernel_boot_id')
        and health.get('platform_contact') is True
        and type(observed) in (int, float) and math.isfinite(observed)
        and record['started_uptime'] <= observed <= now
        and observed <= record['deadline_uptime']
        and now - observed <= CONFIRM_SECONDS
    )


def rollback(install_dir, record, reason):
    """Persist intent first so interrupted rollback resumes on the next tick."""
    record.update(phase='rolling_back', last_error=reason)
    atomic_json(install_dir / JOURNAL, record)
    try:
        previous = record.get('previous')
        if previous is None:
            _run(['systemctl', 'stop', 'ems-device.service'])
            record['phase'] = 'rollback_failed'
            record['last_error'] = 'no_previous_release'
        else:
            target = _release(install_dir, previous)
            replace_link(install_dir / 'current', target)
            _run(['systemctl', 'restart', 'ems-device.service'])
            for _ in range(5):
                time.sleep(1)
                _run(['systemctl', 'is-active', '--quiet', 'ems-device.service'])
            record['phase'] = 'rolled_back'
    except Exception:
        record.update(phase='rollback_failed', last_error='rollback_start_failed')
    atomic_json(install_dir / JOURNAL, record)
    return record['phase']


def inspect_update(install_dir=INSTALL_DIR):
    """Sanitized, read-only local diagnostic; contains no URLs or credentials."""
    result = {}
    for name in ('current', 'previous'):
        link = install_dir / name
        result[name] = str(link.resolve()) if link.is_symlink() else None
    try:
        record = read_json(install_dir / JOURNAL)
        result.update({key: record.get(key) for key in ('attempt_id', 'phase', 'version', 'last_error')})
    except FileNotFoundError:
        result['phase'] = 'idle'
    except (OSError, ValueError):
        result.update(phase='unknown', last_error='journal_unreadable')
    return result


def recover(install_dir=INSTALL_DIR):
    install_dir = install_dir.resolve()
    try:
        with deployment_lock(install_dir):
            try:
                record = read_json(install_dir / JOURNAL)
            except FileNotFoundError:
                return 'idle'
            if record.get('schema') != 1:
                raise ValueError('unsupported_update_journal')
            phase = record.get('phase')
            if phase in {'confirmed', 'rolled_back', 'rollback_failed'}:
                return phase
            if phase == 'rolling_back':
                return rollback(install_dir, record, record.get('last_error') or 'interrupted_rollback')
            if phase not in {'switching', 'awaiting_confirmation'}:
                raise ValueError('invalid_update_phase')
            try:
                target = _release(install_dir, record['release'])
            except ValueError:
                return rollback(install_dir, record, 'release_unavailable')
            if (install_dir / 'current').resolve() != target:
                return rollback(install_dir, record, 'interrupted_activation')
            try:
                health = read_json(Path(record['state_dir']) / HEALTH)
            except (OSError, ValueError):
                health = {}
            now, boot = time.monotonic(), kernel_boot_id()
            if _confirmed(record, health, now, boot):
                try:
                    _run(['systemctl', 'is-active', '--quiet', 'ems-device.service'])
                    pid = _service_pid()
                except (OSError, ValueError, subprocess.SubprocessError):
                    pid = 0
                if type(health.get('pid')) is int and pid > 0 and health['pid'] == pid:
                    record.update(phase='confirmed', last_known_good=record['release'], last_error=None)
                    atomic_json(install_dir / JOURNAL, record)
                    return 'confirmed'
            if boot != record['kernel_boot_id']:
                return rollback(install_dir, record, 'reboot_before_confirmation')
            if now >= record['deadline_uptime']:
                return rollback(install_dir, record, 'confirmation_timeout')
            return 'awaiting_confirmation'
    except FileNotFoundError:
        return 'idle'
    except BlockingIOError:
        return 'busy'


def main():
    parser = argparse.ArgumentParser(description='Recover or confirm a local EMS update')
    parser.add_argument('--install-dir', type=Path, default=INSTALL_DIR)
    parser.add_argument('--check-idle', action='store_true')
    parser.add_argument('--retry-rollback', action='store_true', help='retry recovery after an operator repairs the previous release')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('The update watchdog must run as root')
    if args.check_idle:
        if inspect_update(args.install_dir)['phase'] not in {'idle', 'confirmed', 'rolled_back'}:
            raise SystemExit('Previous update requires confirmation or recovery')
        return
    try:
        if args.retry_rollback:
            install_dir = args.install_dir.resolve()
            with deployment_lock(install_dir):
                record = read_json(install_dir / JOURNAL)
                if record.get('phase') not in {'rolling_back', 'rollback_failed'}:
                    raise ValueError('no_failed_rollback')
                print(rollback(install_dir, record, 'operator_recovery'))
            return
        print(recover(args.install_dir))
    except Exception as exc:
        # Never print untrusted JSON, paths or exception bodies as root logs.
        raise SystemExit(f'update_watchdog_failed type={type(exc).__name__}') from None


if __name__ == '__main__':
    main()
