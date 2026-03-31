"""Tests for server_watchdog.llm."""

from unittest.mock import MagicMock, patch

import pytest

from server_watchdog.config import Config
from server_watchdog.llm import (
    ANALYSIS_PROMPT_TEMPLATE,
    MAINTENANCE_REPORT_PROMPT_TEMPLATE,
    analyse_avc_denials,
    analyse_maintenance_report,
)


def _make_config(**overrides):
    cfg = Config(config_path="/tmp/does_not_exist_watchdog.ini")
    for section_key, value in overrides.items():
        section, key = section_key.split(".", 1)
        cfg._parser.set(section, key, value)
    return cfg


class TestAnalyseAVCDenials:
    def test_no_api_key_returns_notice(self):
        cfg = _make_config(**{"llm.api_key": ""})
        result = analyse_avc_denials(cfg, ["avc: denied { read }"])
        assert "unavailable" in result.lower()

    def test_unknown_provider_returns_notice(self):
        cfg = _make_config(**{"llm.provider": "openai", "llm.api_key": "key"})
        result = analyse_avc_denials(cfg, ["avc: denied { read }"])
        assert "unknown provider" in result.lower()

    def test_gemini_called_with_denials(self):
        cfg = _make_config(**{"llm.api_key": "fake-key", "llm.model": "gemini-1.5-pro"})

        with patch("server_watchdog.llm._call_gemini", return_value="## Analysis\n\nLooks fine.") as mock_call:
            result = analyse_avc_denials(cfg, ["avc: denied { open }"])

        assert result == "## Analysis\n\nLooks fine."
        mock_call.assert_called_once()
        api_key_arg, model_arg, prompt_arg = mock_call.call_args[0]
        assert api_key_arg == "fake-key"
        assert model_arg == "gemini-1.5-pro"
        assert "avc: denied { open }" in prompt_arg

    def test_gemini_api_error_returns_notice(self):
        cfg = _make_config(**{"llm.api_key": "fake-key"})

        with patch("server_watchdog.llm._call_gemini", return_value="(LLM analysis failed: quota exceeded)"):
            result = analyse_avc_denials(cfg, ["avc: denied { write }"])

        assert "failed" in result.lower()

    def test_prompt_contains_all_denials(self):
        cfg = _make_config(**{"llm.api_key": "k"})
        denials = ["denial one", "denial two", "denial three"]

        with patch("server_watchdog.llm._call_gemini", return_value="ok") as mock_call:
            analyse_avc_denials(cfg, denials)

        _, _, prompt = mock_call.call_args[0]
        for denial in denials:
            assert denial in prompt

    def test_prompt_template_has_required_instructions(self):
        prompt = ANALYSIS_PROMPT_TEMPLATE.format(raw_denials="dummy denial")
        assert "Summary" in prompt
        assert "Severity" in prompt
        assert "Recommended Action" in prompt
        assert "audit2allow" in prompt


# ---------------------------------------------------------------------------
# analyse_maintenance_report
# ---------------------------------------------------------------------------

def _make_raw(**overrides):
    """Return a minimal raw data dict for analyse_maintenance_report tests."""
    base = {
        "hostname": "testhost",
        "timestamp": "2026-01-01 12:00:00",
        "server_context": "EDA workstation",
        "uid_map": {0: "root", 1000: "testuser"},
        "packages": {"updates": [], "error": None},
        "services": {"failed": [], "logs": {}, "error": None},
        "storage": {
            "filesystems": [], "nfs_filesystems": [],
            "all_output": "", "threshold": 80, "error": None,
        },
        "journal_errors": {"errors": [], "error": None},
        "coredumps": {"dumps": [], "error": None},
        "threshold": 80,
        "lookback": 30,
        "coredump_age": 45,
    }
    base.update(overrides)
    return base


class TestAnalyseMaintenanceReport:
    def test_no_api_key_returns_notice(self):
        cfg = _make_config(**{"llm.api_key": ""})
        result = analyse_maintenance_report(cfg, _make_raw())
        assert "unavailable" in result.lower()

    def test_unknown_provider_returns_notice(self):
        cfg = _make_config(**{"llm.provider": "openai", "llm.api_key": "key"})
        result = analyse_maintenance_report(cfg, _make_raw())
        assert "unknown provider" in result.lower()

    def test_gemini_called_with_raw_data(self):
        cfg = _make_config(**{"llm.api_key": "fake-key", "llm.model": "gemini-1.5-pro"})
        raw = _make_raw(packages={"updates": ["bash.x86_64 5.1.8 baseos"], "error": None})

        with patch("server_watchdog.llm._call_gemini", return_value="## ✅ Healthy\n\nAll good.") as mock_call:
            result = analyse_maintenance_report(cfg, raw)

        assert result == "## ✅ Healthy\n\nAll good."
        mock_call.assert_called_once()
        _, _, prompt = mock_call.call_args[0]
        assert "bash.x86_64" in prompt

    def test_prompt_includes_server_context(self):
        cfg = _make_config(**{"llm.api_key": "key"})
        raw = _make_raw(server_context="EDA workstation, VNC only, no Bluetooth")

        with patch("server_watchdog.llm._call_gemini", return_value="ok") as mock_call:
            analyse_maintenance_report(cfg, raw)

        _, _, prompt = mock_call.call_args[0]
        assert "EDA workstation" in prompt
        assert "VNC" in prompt

    def test_prompt_includes_uid_map(self):
        cfg = _make_config(**{"llm.api_key": "key"})
        raw = _make_raw(uid_map={0: "root", 1000: "mbuteler"})

        with patch("server_watchdog.llm._call_gemini", return_value="ok") as mock_call:
            analyse_maintenance_report(cfg, raw)

        _, _, prompt = mock_call.call_args[0]
        assert "mbuteler" in prompt

    def test_prompt_includes_nfs_label(self):
        cfg = _make_config(**{"llm.api_key": "key"})
        raw = _make_raw(storage={
            "filesystems": ["/dev/sda1 xfs 20G 16G 4G 82% /"],
            "nfs_filesystems": ["server:/exp nfs4 100G 96G 4G 96% /mnt/nfs"],
            "all_output": "", "threshold": 80, "error": None,
        })

        with patch("server_watchdog.llm._call_gemini", return_value="ok") as mock_call:
            analyse_maintenance_report(cfg, raw)

        _, _, prompt = mock_call.call_args[0]
        assert "NFS" in prompt or "nfs" in prompt.lower()
        assert "lower priority" in prompt

    def test_prompt_includes_coredumps(self):
        cfg = _make_config(**{"llm.api_key": "key"})
        raw = _make_raw(coredumps={
            "dumps": ["Mon 2026-01-15 10:00:00 UTC 1234 1000 SIGSEGV present /usr/bin/myapp 56K"],
            "error": None,
        })

        with patch("server_watchdog.llm._call_gemini", return_value="ok") as mock_call:
            analyse_maintenance_report(cfg, raw)

        _, _, prompt = mock_call.call_args[0]
        assert "/usr/bin/myapp" in prompt

    def test_prompt_includes_failed_service_logs(self):
        cfg = _make_config(**{"llm.api_key": "key"})
        raw = _make_raw(services={
            "failed": ["myapp.service  loaded failed failed My App"],
            "logs": {"myapp.service": "Jan 01 10:00:00 host myapp[123]: segfault"},
            "error": None,
        })

        with patch("server_watchdog.llm._call_gemini", return_value="ok") as mock_call:
            analyse_maintenance_report(cfg, raw)

        _, _, prompt = mock_call.call_args[0]
        assert "myapp.service" in prompt
        assert "segfault" in prompt

    def test_maintenance_prompt_template_has_required_sections(self):
        prompt = MAINTENANCE_REPORT_PROMPT_TEMPLATE.format(
            server_context="test server",
            hostname="host",
            timestamp="2026-01-01",
            uid_map_text="UID 0: root",
            raw_data_text="## Package Updates\nNone",
        )
        assert "PACKAGE UPDATES" in prompt
        assert "FAILED SERVICES" in prompt
        assert "STORAGE" in prompt
        assert "NFS" in prompt
        assert "COREDUMP" in prompt
