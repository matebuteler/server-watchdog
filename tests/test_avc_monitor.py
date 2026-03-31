"""Tests for server_watchdog.avc_monitor."""

import threading
import time
from unittest.mock import MagicMock, call, patch

import pytest

from server_watchdog.avc_monitor import AVCMonitor, _markdown_to_html, read_current_avc_denials
from server_watchdog.config import Config
from server_watchdog.utils import escape_html


def _make_config(**overrides):
    cfg = Config(config_path="/tmp/does_not_exist_watchdog.ini")
    for section_key, value in overrides.items():
        section, key = section_key.split(".", 1)
        cfg._parser.set(section, key, value)
    return cfg


class TestAVCMonitorEnqueue:
    """Unit tests for the batching / queuing logic."""

    def test_first_denial_starts_timer(self):
        cfg = _make_config(**{"avc_monitor.batch_interval": "5"})
        monitor = AVCMonitor(cfg)

        with patch.object(monitor, "_flush") as mock_flush:
            monitor._enqueue("avc: denied { read }")
            assert monitor._timer is not None

    def test_multiple_denials_only_one_timer(self):
        cfg = _make_config(**{"avc_monitor.batch_interval": "60"})
        monitor = AVCMonitor(cfg)

        monitor._enqueue("avc: denied { read }")
        t1 = monitor._timer
        monitor._enqueue("avc: denied { write }")
        t2 = monitor._timer

        assert t1 is t2  # same timer object – not restarted
        t1.cancel()

    def test_flush_clears_pending(self):
        cfg = _make_config(**{"avc_monitor.batch_interval": "60"})
        monitor = AVCMonitor(cfg)

        with (
            patch.object(monitor, "_send_alert"),
            patch("server_watchdog.avc_monitor.analyse_avc_denials", return_value="analysis"),
        ):
            monitor._enqueue("avc: denied { read }")
            monitor._timer.cancel()
            monitor._flush()

        assert monitor._pending == []
        assert monitor._timer is None

    def test_flush_calls_send_alert(self):
        cfg = _make_config(**{"avc_monitor.batch_interval": "60"})
        monitor = AVCMonitor(cfg)

        denial = "avc: denied { open } for pid=1234 comm=nginx"
        monitor._pending = [denial]

        with (
            patch.object(monitor, "_send_alert") as mock_alert,
            patch("server_watchdog.avc_monitor.analyse_avc_denials", return_value="analysis"),
        ):
            monitor._flush()

        mock_alert.assert_called_once()
        denials_arg, analysis_arg = mock_alert.call_args[0]
        assert denial in denials_arg
        assert analysis_arg == "analysis"

    def test_flush_empty_pending_does_nothing(self):
        cfg = _make_config()
        monitor = AVCMonitor(cfg)

        with patch.object(monitor, "_send_alert") as mock_alert:
            monitor._flush()

        mock_alert.assert_not_called()


class TestSendAlert:
    def test_send_alert_sends_email(self):
        cfg = _make_config()
        monitor = AVCMonitor(cfg)

        with patch("server_watchdog.avc_monitor.send_email") as mock_send:
            monitor._send_alert(["avc: denied { read }"], "LLM result here")

        assert mock_send.called
        subject = mock_send.call_args[0][1]
        assert "AVC" in subject

    def test_send_alert_includes_raw_denials_in_email(self):
        cfg = _make_config()
        monitor = AVCMonitor(cfg)
        denial = "avc: denied { write } for pid=42"

        with patch("server_watchdog.avc_monitor.send_email") as mock_send:
            monitor._send_alert([denial], "analysis text")

        body_text = mock_send.call_args[0][2]
        assert denial in body_text

    def test_send_alert_includes_analysis(self):
        cfg = _make_config()
        monitor = AVCMonitor(cfg)

        with patch("server_watchdog.avc_monitor.send_email") as mock_send:
            monitor._send_alert(["denial"], "LLM says: Critical issue")

        body_text = mock_send.call_args[0][2]
        assert "LLM says: Critical issue" in body_text


class TestHelpers:
    def test_escape_html_ampersand(self):
        assert escape_html("a & b") == "a &amp; b"

    def test_escape_html_lt_gt(self):
        assert escape_html("<script>") == "&lt;script&gt;"

    def test_markdown_to_html_bold(self):
        result = _markdown_to_html("**important**")
        assert "<strong>important</strong>" in result

    def test_markdown_to_html_heading(self):
        result = _markdown_to_html("## Section Title")
        assert "<h3>" in result
        assert "Section Title" in result

    def test_markdown_to_html_bullet(self):
        result = _markdown_to_html("- list item")
        assert "<li>" in result
        assert "list item" in result


# ---------------------------------------------------------------------------
# read_current_avc_denials
# ---------------------------------------------------------------------------

import json
import subprocess


def _journal_output(*messages):
    """Build fake journalctl stdout bytes containing the given MESSAGE values."""
    lines = [json.dumps({"MESSAGE": m}) for m in messages]
    return "\n".join(lines).encode()


class TestReadCurrentAvcDenials:
    def _cfg(self, **overrides):
        cfg = _make_config(**overrides)
        return cfg

    def test_returns_only_avc_lines(self):
        journal_bytes = _journal_output(
            "avc: denied { read } for pid=1234",
            "normal kernel message",
            "AVC: denied { write } for pid=5678",   # uppercase AVC also matches
        )
        completed = MagicMock()
        completed.stdout = journal_bytes
        completed.returncode = 0

        with patch("subprocess.run", return_value=completed):
            denials = read_current_avc_denials(self._cfg())

        assert len(denials) == 2
        assert all("denied" in d.lower() for d in denials)

    def test_empty_journal_returns_empty_list(self):
        completed = MagicMock()
        completed.stdout = b""
        completed.returncode = 0

        with patch("subprocess.run", return_value=completed):
            denials = read_current_avc_denials(self._cfg())

        assert denials == []

    def test_journalctl_not_found_returns_empty_list(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            denials = read_current_avc_denials(self._cfg())

        assert denials == []

    def test_journalctl_timeout_returns_empty_list(self):
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired("journalctl", 60)):
            denials = read_current_avc_denials(self._cfg())

        assert denials == []

    def test_respects_avc_lookback_days(self):
        completed = MagicMock()
        completed.stdout = b""
        completed.returncode = 0

        with patch("subprocess.run", return_value=completed) as mock_run:
            read_current_avc_denials(self._cfg(**{"avc_monitor.avc_lookback_days": "14"}))

        cmd_args = mock_run.call_args[0][0]
        assert any("14 days ago" in arg for arg in cmd_args)

    def test_malformed_json_lines_are_skipped(self):
        bad_output = b'not-json\n{"MESSAGE": "avc: denied { open }"}\nnot-json\n'
        completed = MagicMock()
        completed.stdout = bad_output
        completed.returncode = 0

        with patch("subprocess.run", return_value=completed):
            denials = read_current_avc_denials(self._cfg())

        assert len(denials) == 1
        assert "avc: denied" in denials[0]

