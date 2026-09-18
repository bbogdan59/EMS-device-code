import os

from ems_device import system_stats


def test_cpu_load_1m_rounds_to_two_decimals(monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.4567, 0.3, 0.2))
    assert system_stats._cpu_load_1m() == 0.46


def test_cpu_load_1m_absent_when_unavailable(monkeypatch):
    def _raise():
        raise OSError("not supported on this platform")
    monkeypatch.setattr(os, "getloadavg", _raise)
    assert system_stats._cpu_load_1m() is None


def test_memory_stats_computes_used_percent_from_meminfo(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        1024000 kB\nMemAvailable:     512000 kB\nSwapTotal:             0 kB\n")
    result = system_stats._memory_stats(str(meminfo))
    assert result == {"memory_total_mb": 1000.0, "memory_used_percent": 50.0}


def test_memory_stats_omits_used_percent_when_available_missing(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        1024000 kB\n")
    result = system_stats._memory_stats(str(meminfo))
    assert result == {"memory_total_mb": 1000.0}
    assert "memory_used_percent" not in result


def test_memory_stats_empty_when_file_missing(tmp_path):
    assert system_stats._memory_stats(str(tmp_path / "does-not-exist")) == {}


def test_memory_stats_empty_when_unparseable(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: not-a-number kB\n")
    assert system_stats._memory_stats(str(meminfo)) == {}


def test_temperature_c_reads_millidegrees(tmp_path, monkeypatch):
    thermal = tmp_path / "temp"
    thermal.write_text("46521\n")
    monkeypatch.setattr(system_stats, "THERMAL_ZONE", thermal)
    assert system_stats._temperature_c() == 46.5


def test_temperature_c_absent_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(system_stats, "THERMAL_ZONE", tmp_path / "does-not-exist")
    assert system_stats._temperature_c() is None


def test_disk_used_percent_reads_real_root():
    # Uses the real filesystem -- just asserts the shape, not a fabricated value.
    result = system_stats._disk_used_percent("/")
    assert result is None or (isinstance(result, float) and 0 <= result <= 100)


def test_disk_used_percent_absent_for_nonexistent_path():
    assert system_stats._disk_used_percent("/no/such/path/at/all") is None


def test_collect_omits_all_fields_when_nothing_readable(monkeypatch):
    monkeypatch.setattr(system_stats, "_cpu_load_1m", lambda: None)
    monkeypatch.setattr(system_stats, "_memory_stats", lambda: {})
    monkeypatch.setattr(system_stats, "_temperature_c", lambda: None)
    monkeypatch.setattr(system_stats, "_disk_used_percent", lambda: None)
    assert system_stats.collect() == {}


def test_collect_merges_all_available_fields(monkeypatch):
    monkeypatch.setattr(system_stats, "_cpu_load_1m", lambda: 0.5)
    monkeypatch.setattr(system_stats, "_memory_stats", lambda: {"memory_total_mb": 2000.0, "memory_used_percent": 30.0})
    monkeypatch.setattr(system_stats, "_temperature_c", lambda: 45.0)
    monkeypatch.setattr(system_stats, "_disk_used_percent", lambda: 10.0)
    assert system_stats.collect() == {
        "cpu_load_1m": 0.5, "memory_total_mb": 2000.0, "memory_used_percent": 30.0,
        "temperature_c": 45.0, "disk_used_percent": 10.0,
    }
