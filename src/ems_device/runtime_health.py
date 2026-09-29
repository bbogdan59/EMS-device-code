"""Evidence from this process after a successful platform round trip."""
from pathlib import Path
import os
import sys
import time

from . import __version__
from .update_watchdog import HEALTH, INSTALL_DIR, JOURNAL, atomic_json, kernel_boot_id, read_json

# Capture once at import. Resolving current after an update could identify an
# old process as the new release even though its interpreter never restarted.
PROCESS_RELEASE = str(Path(sys.prefix).resolve().parent)


def record_contact(state_dir, boot_id, *, install_dir=INSTALL_DIR):
    attempt = None
    try:
        update = read_json(install_dir / JOURNAL)
        if update.get('release') == PROCESS_RELEASE and update.get('version') == __version__:
            attempt = update.get('attempt_id')
    except (OSError, ValueError):
        pass
    atomic_json(state_dir / HEALTH, {
        'boot_id': boot_id, 'version': __version__, 'release': PROCESS_RELEASE,
        'pid': os.getpid(),
        'kernel_boot_id': kernel_boot_id(), 'attempt_id': attempt,
        'observed_uptime': time.monotonic(), 'platform_contact': True,
    }, mode=0o600)
