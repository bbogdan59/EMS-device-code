"""Best-effort clone detection using public board metadata, never authentication.

The random EMS serial remains stable across OS updates. Unsupported hardware
has no fabricated fingerprint; machine-id/MAC addresses are deliberately unused.
"""
import hashlib
from pathlib import Path
import re

DEVICE_TREE_ROOTS = (Path('/sys/firmware/devicetree/base'), Path('/proc/device-tree'))


class HardwareIdentityError(ValueError):
    """A persisted device identity cannot be used on this observed board."""


def _text(path):
    with path.open('rb') as stream:
        data = stream.read(257)
    if len(data) > 256:
        raise ValueError('invalid_hardware_metadata')
    return data.rstrip(b'\x00\r\n ').decode('ascii')


def observe():
    for root in DEVICE_TREE_ROOTS:
        try:
            model = _text(root / 'model')
            serial = _text(root / 'serial-number').lower()
        except (OSError, ValueError):
            continue
        if not model.startswith('Raspberry Pi'):
            continue
        if not re.fullmatch(r'[0-9a-f]{8,16}', serial) or int(serial, 16) == 0:
            continue
        # Normalize the 32/64-bit hexadecimal representation without inventing
        # a different identity when the same serial gains leading zeros.
        serial = f'{int(serial, 16):016x}'
        source = 'raspberry_pi_serial'
        return {'source': source, 'fingerprint': hashlib.sha256(f'{source}:{serial}'.encode()).hexdigest()}
    return None


def status(bound, observed):
    if bound is None:
        state = 'unbound' if observed else 'unavailable'
    elif observed is None:
        state = 'unavailable'
    else:
        state = 'matched' if bound == observed else 'mismatch'
    return {'status': state, 'source': (observed or bound or {}).get('source'),
            'bound_fingerprint': (bound or {}).get('fingerprint'),
            'observed_fingerprint': (observed or {}).get('fingerprint')}
