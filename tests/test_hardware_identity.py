import hashlib

import pytest

from ems_device import hardware_identity
from ems_device.state import State


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(hardware_identity, 'DEVICE_TREE_ROOTS', (tmp_path,))
    (tmp_path / 'model').write_bytes(b'Raspberry Pi 4 Model B Rev 1.5\x00')
    (tmp_path / 'serial-number').write_bytes(b'00000000aabbccdd\x00')
    return tmp_path


def test_board_fingerprint_is_stable_public_metadata(board):
    expected = {'source': 'raspberry_pi_serial',
                'fingerprint': hashlib.sha256(b'raspberry_pi_serial:00000000aabbccdd').hexdigest()}
    assert hardware_identity.observe() == expected
    (board / 'serial-number').write_bytes(b'AABBCCDD\n')
    assert hardware_identity.observe() == expected


@pytest.mark.parametrize('serial', [b'', b'0000000000000000', b'not-a-serial', b'x' * 300, b'\xff' * 16])
def test_invalid_or_missing_serial_is_unknown(board, serial):
    (board / 'serial-number').write_bytes(serial)
    assert hardware_identity.observe() is None


def test_non_pi_does_not_adopt_unverified_serial_semantics(board):
    (board / 'model').write_bytes(b'Other board')
    assert hardware_identity.observe() is None
    (board / 'serial-number').unlink()
    assert hardware_identity.observe() is None


def test_state_binding_survives_restart_and_soft_reset(board, tmp_path):
    observed = hardware_identity.observe()
    path = tmp_path / 'state'
    state = State(path)
    identity = state.identity()
    state.bind_hardware(observed)
    state.close()
    state = State(path)
    state.bind_hardware(observed)
    state.clear_assignment()
    assert state.get('hardware_identity') == observed
    assert state.identity()['serial_number'] == identity['serial_number']
    state.close()


@pytest.mark.parametrize('observed', [None, {'source': 'raspberry_pi_serial', 'fingerprint': 'another-board'}])
def test_bound_identity_cannot_move_or_silently_lose_hardware_source(board, tmp_path, observed):
    state = State(tmp_path / 'state')
    state.bind_hardware(hardware_identity.observe())
    original = state.identity()
    with pytest.raises(hardware_identity.HardwareIdentityError):
        state.bind_hardware(observed)
    assert state.identity() == original
    state.close()


def test_factory_reset_replaces_hardware_binding_and_all_secrets_atomically(board, tmp_path):
    old_board = hardware_identity.observe()
    state = State(tmp_path / 'state')
    state.bind_hardware(old_board)
    identity = state.identity()
    new_board = {'source': old_board['source'], 'fingerprint': 'new-board'}
    state.factory_reset(hardware_identity=new_board)
    assert state.get('hardware_identity') == new_board
    assert state.identity()['provisioning_secret'] != identity['provisioning_secret']
    assert state.identity()['serial_number'] != identity['serial_number']
    state.close()


def test_unavailable_hardware_does_not_invent_identity_binding(tmp_path):
    state = State(tmp_path)
    identity = state.identity()
    state.bind_hardware(None)
    assert state.get('hardware_identity') is None
    assert state.identity() == identity
    assert hardware_identity.status(None, None)['status'] == 'unavailable'
    state.close()
