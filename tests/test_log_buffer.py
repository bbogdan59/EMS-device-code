import logging

from ems_device.log_buffer import CompactLogBuffer


def _record(level, message, *args):
    return logging.LogRecord(
        name="ems_device", level=level, pathname=__file__, lineno=1, msg=message, args=args, exc_info=None,
    )


def test_captures_warning_with_code_and_detail():
    buffer = CompactLogBuffer()
    buffer.emit(_record(logging.WARNING, "sample_failed type=%s", "ValueError"))
    entries = buffer.drain()
    assert len(entries) == 1
    assert entries[0]["level"] == "warning"
    assert entries[0]["code"] == "sample_failed"
    assert entries[0]["detail"] == "type=ValueError"
    assert "occurred_at" in entries[0]


def test_error_level_maps_to_error():
    buffer = CompactLogBuffer()
    buffer.emit(_record(logging.ERROR, "agent_stopped type=%s", "ValueError"))
    assert buffer.drain()[0]["level"] == "error"


def test_debug_and_info_are_never_captured_handler_level_gate():
    """The handler itself is installed at WARNING (see cli.py); this test
    documents that emit() is never called for lower levels via the normal
    logging dispatch path, by checking the handler's own level."""
    buffer = CompactLogBuffer()
    assert buffer.level == logging.WARNING


def test_message_with_no_space_becomes_code_only():
    buffer = CompactLogBuffer()
    buffer.emit(_record(logging.WARNING, "enrollment_offline"))
    entries = buffer.drain()
    assert entries[0]["code"] == "enrollment_offline"
    assert "detail" not in entries[0]


def test_code_and_detail_are_truncated_to_stay_compact():
    buffer = CompactLogBuffer()
    buffer.emit(_record(logging.WARNING, "x" * 100 + " " + "y" * 300))
    entries = buffer.drain()
    assert len(entries[0]["code"]) == 64
    assert len(entries[0]["detail"]) == 200


def test_drain_clears_the_buffer():
    buffer = CompactLogBuffer()
    buffer.emit(_record(logging.WARNING, "a"))
    assert len(buffer.drain()) == 1
    assert buffer.drain() == []


def test_ring_buffer_drops_oldest_beyond_capacity():
    buffer = CompactLogBuffer(capacity=2)
    for i in range(5):
        buffer.emit(_record(logging.WARNING, f"code{i}"))
    entries = buffer.drain()
    assert [e["code"] for e in entries] == ["code3", "code4"]


def test_broken_format_string_never_raises():
    buffer = CompactLogBuffer()
    # A non-empty args tuple that doesn't match the format string makes
    # getMessage() raise (unlike an empty args tuple, which is left unformatted).
    broken = _record(logging.WARNING, "needs_two %s %s", "only-one")
    buffer.emit(broken)  # must swallow, not propagate
    assert buffer.drain() == []
