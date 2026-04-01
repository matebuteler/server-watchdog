"""Tests for server_watchdog.bottleneck."""

import json
import time
from pathlib import Path
from unittest.mock import mock_open, patch

import pytest

from server_watchdog.bottleneck import (
    DEFAULT_LOOKBACK_DAYS,
    _MIN_IMPACT_SCORE,
    _VNC_WEIGHTS,
    _sample_impact,
    analyze_bottlenecks,
    take_sample,
)


# ── _sample_impact ────────────────────────────────────────────────────────────

class TestSampleImpact:
    def test_below_threshold_is_zero(self):
        assert _sample_impact(15.0, threshold=20.0, vnc_weight=1.0) == 0.0

    def test_exactly_at_threshold_is_zero(self):
        # The threshold itself produces 0 excess → 0 impact
        assert _sample_impact(20.0, threshold=20.0, vnc_weight=1.0) == 0.0

    def test_full_saturation_returns_weight(self):
        # metric_pct = 100 → excess = 100 - threshold; impact = weight
        result = _sample_impact(100.0, threshold=20.0, vnc_weight=1.0)
        assert result == pytest.approx(1.0)

    def test_mid_range_value(self):
        # threshold=20, pct=60: excess=40 out of headroom 80 → 0.5 * weight
        result = _sample_impact(60.0, threshold=20.0, vnc_weight=1.0)
        assert result == pytest.approx(0.5)

    def test_vnc_weight_applied(self):
        # Same excess but different VNC weight
        r1 = _sample_impact(60.0, threshold=20.0, vnc_weight=1.0)
        r2 = _sample_impact(60.0, threshold=20.0, vnc_weight=0.5)
        assert r2 == pytest.approx(r1 * 0.5)

    def test_capped_at_weight_not_above(self):
        # Even if pct > 100 were somehow passed, impact must not exceed weight
        result = _sample_impact(150.0, threshold=20.0, vnc_weight=0.9)
        assert result <= 0.9 + 1e-9

    def test_swap_weight_equals_iowait_weight(self):
        assert _VNC_WEIGHTS["Swap"] == _VNC_WEIGHTS["IO wait"]

    def test_cpu_weight_less_than_iowait(self):
        assert _VNC_WEIGHTS["CPU"] < _VNC_WEIGHTS["IO wait"]


# ── take_sample ───────────────────────────────────────────────────────────────

class TestTakeSample:
    _MEM_CONTENT = (
        "MemTotal:       8000000 kB\n"
        "MemAvailable:   4000000 kB\n"
        "SwapTotal:      2000000 kB\n"
        "SwapFree:       2000000 kB\n"
    )

    def test_sample_written_to_file(self, tmp_path):
        data_file = str(tmp_path / "bottleneck.jsonl")

        stat_seq = iter([
            "cpu  1000 0 500 8000 200 0 0 0 0 0\ncpu0 x\n",
            "cpu  1100 0 550 8100 250 0 0 0 0 0\ncpu0 x\n",
        ])

        def fake_open(path, mode="r", *args, **kwargs):
            if "stat" in path:
                return mock_open(read_data=next(stat_seq))()
            if "meminfo" in path:
                return mock_open(read_data=self._MEM_CONTENT)()
            return open(path, mode, *args, **kwargs)  # pylint: disable=unspecified-encoding

        with patch("server_watchdog.bottleneck.open", side_effect=fake_open), \
             patch("server_watchdog.bottleneck.time.sleep"):
            take_sample(data_file=data_file, sample_interval=0)

        lines = Path(data_file).read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        for key in ("ts", "cpu", "iowait", "mem", "swap"):
            assert key in record

    def test_missing_proc_stat_does_not_create_file(self, tmp_path):
        data_file = str(tmp_path / "bottleneck.jsonl")
        with patch("server_watchdog.bottleneck.open", side_effect=OSError("no /proc")), \
             patch("server_watchdog.bottleneck.time.sleep"):
            take_sample(data_file=data_file, sample_interval=0)
        assert not Path(data_file).exists()

    def test_multiple_samples_appended(self, tmp_path):
        data_file = str(tmp_path / "bottleneck.jsonl")
        stat_seq = iter([
            "cpu  1000 0 500 8000 200 0 0 0\n",
            "cpu  1100 0 550 8100 250 0 0 0\n",
            "cpu  1200 0 600 8200 300 0 0 0\n",
            "cpu  1300 0 650 8300 350 0 0 0\n",
        ])

        def fake_open(path, mode="r", *args, **kwargs):
            if "stat" in path:
                return mock_open(read_data=next(stat_seq))()
            if "meminfo" in path:
                return mock_open(read_data=self._MEM_CONTENT)()
            return open(path, mode, *args, **kwargs)  # pylint: disable=unspecified-encoding

        with patch("server_watchdog.bottleneck.open", side_effect=fake_open), \
             patch("server_watchdog.bottleneck.time.sleep"):
            take_sample(data_file=data_file, sample_interval=0)
            take_sample(data_file=data_file, sample_interval=0)

        assert len(Path(data_file).read_text().splitlines()) == 2


# ── analyze_bottlenecks ───────────────────────────────────────────────────────

class TestAnalyzeBottlenecks:
    def _write_samples(self, path, records):
        with open(path, "w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def test_missing_file_returns_error(self, tmp_path):
        result = analyze_bottlenecks(str(tmp_path / "missing.jsonl"), lookback_days=14)
        assert result["error"] is not None
        assert result["bottlenecks"] == []

    def test_no_samples_in_window_returns_error(self, tmp_path):
        data_file = tmp_path / "b.jsonl"
        old_ts = int(time.time()) - 30 * 86400
        self._write_samples(str(data_file), [
            {"ts": old_ts, "cpu": 85.0, "iowait": 80.0, "mem": 50.0, "swap": 0.0}
        ])
        result = analyze_bottlenecks(str(data_file), lookback_days=14)
        assert result["error"] is not None
        assert result["total_samples"] == 0

    def test_healthy_samples_produce_no_bottlenecks(self, tmp_path):
        data_file = tmp_path / "b.jsonl"
        now = int(time.time())
        samples = [
            {"ts": now - i * 300, "cpu": 10.0, "iowait": 5.0,
             "mem": 50.0, "swap": 0.0}
            for i in range(20)
        ]
        self._write_samples(str(data_file), samples)
        result = analyze_bottlenecks(str(data_file), lookback_days=14)
        assert result["error"] is None
        assert result["bottlenecks"] == []
        assert result["total_samples"] == 20

    def test_persistent_high_iowait_produces_high_score(self, tmp_path):
        data_file = tmp_path / "b.jsonl"
        now = int(time.time())
        # All samples have severe IO wait (90 %)
        samples = [
            {"ts": now - i * 300, "cpu": 5.0, "iowait": 90.0,
             "mem": 50.0, "swap": 0.0}
            for i in range(20)
        ]
        self._write_samples(str(data_file), samples)
        result = analyze_bottlenecks(str(data_file), lookback_days=14)
        assert result["error"] is None
        io_entry = next((b for b in result["bottlenecks"] if b["name"] == "IO wait"), None)
        assert io_entry is not None
        # score = _sample_impact(90, 20, 1.0) * 100 = ((90-20)/(100-20)) * 100 = 87.5
        assert io_entry["score"] == pytest.approx(87.5, abs=0.5)

    def test_score_higher_when_metric_more_severe(self, tmp_path):
        """Samples with 90 % iowait must score higher than 30 % iowait."""
        now = int(time.time())

        def _make_file(folder, iowait):
            path = folder / "b.jsonl"
            self._write_samples(str(path), [
                {"ts": now - i * 300, "cpu": 5.0, "iowait": iowait,
                 "mem": 50.0, "swap": 0.0}
                for i in range(10)
            ])
            return str(path)

        tmp1 = tmp_path / "high"
        tmp2 = tmp_path / "low"
        tmp1.mkdir(); tmp2.mkdir()

        r_high = analyze_bottlenecks(_make_file(tmp1, 90.0), lookback_days=14)
        r_low  = analyze_bottlenecks(_make_file(tmp2, 30.0), lookback_days=14)

        score_high = next(b["score"] for b in r_high["bottlenecks"] if b["name"] == "IO wait")
        score_low  = next(b["score"] for b in r_low["bottlenecks"]  if b["name"] == "IO wait")
        assert score_high > score_low

    def test_score_higher_when_metric_more_frequent(self, tmp_path):
        """IO wait above threshold in all samples must outscore partial presence."""
        now = int(time.time())

        def _make_file(folder, n_high, n_total):
            path = folder / "b.jsonl"
            samples = [
                {"ts": now - i * 300, "cpu": 5.0,
                 "iowait": 90.0 if i < n_high else 2.0,
                 "mem": 50.0, "swap": 0.0}
                for i in range(n_total)
            ]
            self._write_samples(str(path), samples)
            return str(path)

        tmp1 = tmp_path / "all"; tmp2 = tmp_path / "half"
        tmp1.mkdir(); tmp2.mkdir()

        r_all  = analyze_bottlenecks(_make_file(tmp1, 10, 10), lookback_days=14)
        r_half = analyze_bottlenecks(_make_file(tmp2, 5,  10), lookback_days=14)

        score_all  = next(b["score"] for b in r_all["bottlenecks"]  if b["name"] == "IO wait")
        score_half = next(b["score"] for b in r_half["bottlenecks"] if b["name"] == "IO wait")
        assert score_all > score_half

    def test_iowait_outscores_cpu_at_equal_excess(self, tmp_path):
        """IO wait weight (1.0) > CPU weight (0.9), so equal excess → IO higher."""
        data_file = tmp_path / "b.jsonl"
        now = int(time.time())
        # iowait at 60 % (threshold 20 %): excess = 40/80 = 0.5
        # cpu at 85 % (threshold 70 %): excess = 15/30 = 0.5
        # Both same normalised excess; IO weight 1.0 vs CPU 0.9
        samples = [
            {"ts": now - i * 300, "cpu": 85.0, "iowait": 60.0,
             "mem": 50.0, "swap": 0.0}
            for i in range(10)
        ]
        self._write_samples(str(data_file), samples)
        result = analyze_bottlenecks(str(data_file), lookback_days=14)
        io_score  = next(b["score"] for b in result["bottlenecks"] if b["name"] == "IO wait")
        cpu_score = next(b["score"] for b in result["bottlenecks"] if b["name"] == "CPU")
        assert io_score > cpu_score

    def test_below_min_impact_score_not_reported(self, tmp_path):
        """Metrics just barely above threshold in few samples stay below _MIN_IMPACT_SCORE."""
        data_file = tmp_path / "b.jsonl"
        now = int(time.time())
        # Only 1 of 100 samples has mild IO wait (25 %):
        # impact per sample = (25-20)/(100-20) = 0.0625; avg = 0.000625; score = 0.0625 %
        samples = [
            {"ts": now - i * 300,
             "cpu": 5.0, "iowait": 25.0 if i == 0 else 2.0,
             "mem": 50.0, "swap": 0.0}
            for i in range(100)
        ]
        self._write_samples(str(data_file), samples)
        result = analyze_bottlenecks(str(data_file), lookback_days=14)
        assert not any(b["name"] == "IO wait" for b in result["bottlenecks"])

    def test_only_recent_samples_counted(self, tmp_path):
        data_file = tmp_path / "b.jsonl"
        now = int(time.time())
        samples = [
            {"ts": now - 86400,     "cpu": 5.0, "iowait": 80.0, "mem": 50.0, "swap": 0.0},
            {"ts": now - 20*86400,  "cpu": 5.0, "iowait": 80.0, "mem": 50.0, "swap": 0.0},
        ]
        self._write_samples(str(data_file), samples)
        result = analyze_bottlenecks(str(data_file), lookback_days=14)
        assert result["total_samples"] == 1

    def test_malformed_lines_skipped(self, tmp_path):
        data_file = tmp_path / "b.jsonl"
        now = int(time.time())
        with open(data_file, "w") as fh:
            fh.write("not-valid-json\n")
            fh.write(json.dumps({"ts": now, "cpu": 5.0, "iowait": 2.0,
                                  "mem": 50.0, "swap": 0.0}) + "\n")
        result = analyze_bottlenecks(str(data_file), lookback_days=14)
        assert result["error"] is None
        assert result["total_samples"] == 1

    def test_bottlenecks_sorted_by_score_descending(self, tmp_path):
        data_file = tmp_path / "b.jsonl"
        now = int(time.time())
        # Severe IO wait and mild CPU — IO must appear first
        samples = [
            {"ts": now - i * 300, "cpu": 85.0, "iowait": 90.0,
             "mem": 50.0, "swap": 0.0}
            for i in range(10)
        ]
        self._write_samples(str(data_file), samples)
        result = analyze_bottlenecks(str(data_file), lookback_days=14)
        scores = [b["score"] for b in result["bottlenecks"]]
        assert scores == sorted(scores, reverse=True)

    def test_result_contains_required_keys(self, tmp_path):
        data_file = tmp_path / "b.jsonl"
        now = int(time.time())
        self._write_samples(str(data_file), [
            {"ts": now, "cpu": 5.0, "iowait": 2.0, "mem": 50.0, "swap": 0.0}
        ])
        result = analyze_bottlenecks(str(data_file), lookback_days=14)
        for key in ("bottlenecks", "total_samples", "lookback_days", "error"):
            assert key in result

