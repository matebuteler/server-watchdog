"""Tests for server_watchdog.llm."""

from unittest.mock import MagicMock, patch

import pytest

from server_watchdog.config import Config
from server_watchdog.llm import analyse_avc_denials, ANALYSIS_PROMPT_TEMPLATE


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
