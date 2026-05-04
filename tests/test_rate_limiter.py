"""Tests for server_watchdog.rate_limiter."""

import json
import os
import time

import pytest

from server_watchdog.rate_limiter import (
    REQUESTS_PER_DAY,
    REQUESTS_PER_MINUTE,
    TOKENS_PER_MINUTE,
    GeminiRateLimiter,
    estimate_tokens,
)


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_returns_positive_for_non_empty_text(self):
        assert estimate_tokens("hello world") > 0

    def test_empty_string_returns_one(self):
        assert estimate_tokens("") == 1

    def test_longer_text_returns_more_tokens(self):
        assert estimate_tokens("a" * 300) > estimate_tokens("a" * 30)


# ---------------------------------------------------------------------------
# GeminiRateLimiter helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def limiter(tmp_path):
    """A fresh limiter backed by a temp-dir state file."""
    return GeminiRateLimiter(state_path=str(tmp_path / "rate_limit_state.json"))


MODEL = "gemini-2.5-flash"
FALLBACK = "gemini-2.5-flash-fallback"


# ---------------------------------------------------------------------------
# within_limits – basic allow/deny
# ---------------------------------------------------------------------------

class TestWithinLimits:
    def test_fresh_limiter_allows_request(self, limiter):
        assert limiter.within_limits(MODEL, 100) is True

    def test_rpm_limit_blocks_after_max_requests(self, limiter):
        for _ in range(REQUESTS_PER_MINUTE):
            limiter.record(MODEL, 10)
        assert limiter.within_limits(MODEL, 10) is False

    def test_rpd_limit_blocks_after_max_daily_requests(self, limiter):
        for _ in range(REQUESTS_PER_DAY):
            limiter.record(MODEL, 10)
        assert limiter.within_limits(MODEL, 10) is False

    def test_tpm_limit_blocks_when_tokens_exceeded(self, limiter):
        # Manually inject a large token count into minute_calls
        limiter._state[MODEL] = {
            "minute_calls": [{"t": time.time(), "tokens": TOKENS_PER_MINUTE - 50}],
            "day_calls": [time.time()],
        }
        # A request of 100 tokens would exceed 250 000
        assert limiter.within_limits(MODEL, 100) is False

    def test_tpm_allows_when_just_under_limit(self, limiter):
        limiter._state[MODEL] = {
            "minute_calls": [{"t": time.time(), "tokens": TOKENS_PER_MINUTE - 200}],
            "day_calls": [time.time()],
        }
        assert limiter.within_limits(MODEL, 100) is True

    def test_models_have_independent_limits(self, limiter):
        for _ in range(REQUESTS_PER_MINUTE):
            limiter.record(MODEL, 10)
        # Primary is exhausted; fallback should still be allowed
        assert limiter.within_limits(MODEL, 10) is False
        assert limiter.within_limits(FALLBACK, 10) is True

    def test_stale_minute_entries_pruned(self, limiter):
        # Insert RPM-limit entries with timestamps > 60 s ago
        old_t = time.time() - 61
        limiter._state[MODEL] = {
            "minute_calls": [{"t": old_t, "tokens": 10}] * REQUESTS_PER_MINUTE,
            "day_calls": [old_t] * REQUESTS_PER_MINUTE,
        }
        assert limiter.within_limits(MODEL, 10) is True

    def test_stale_day_entries_pruned(self, limiter):
        old_t = time.time() - 86401
        limiter._state[MODEL] = {
            "minute_calls": [],
            "day_calls": [old_t] * REQUESTS_PER_DAY,
        }
        assert limiter.within_limits(MODEL, 10) is True


# ---------------------------------------------------------------------------
# record + persistence
# ---------------------------------------------------------------------------

class TestRecord:
    def test_record_increments_counters(self, limiter):
        limiter.record(MODEL, 100)
        ms = limiter._model_state(MODEL)
        assert len(ms["minute_calls"]) == 1
        assert len(ms["day_calls"]) == 1

    def test_record_persists_to_file(self, tmp_path):
        path = str(tmp_path / "state.json")
        lim1 = GeminiRateLimiter(state_path=path)
        lim1.record(MODEL, 50)

        lim2 = GeminiRateLimiter(state_path=path)
        assert len(lim2._model_state(MODEL)["day_calls"]) == 1

    def test_corrupt_state_file_resets_to_empty(self, tmp_path):
        path = str(tmp_path / "state.json")
        with open(path, "w") as fh:
            fh.write("not valid json{{{")
        # Should not raise; falls back to empty state
        lim = GeminiRateLimiter(state_path=path)
        assert lim.within_limits(MODEL, 10) is True


# ---------------------------------------------------------------------------
# Rate limiter integration in _call_gemini
# ---------------------------------------------------------------------------

class TestCallGeminiRateLimiting:
    def _make_google_mock(self, response_text="ok"):
        """Return a (mock_google, mock_genai) pair for patching sys.modules."""
        from unittest.mock import MagicMock
        mock_genai = MagicMock()
        mock_genai.Client.return_value.models.generate_content.return_value.text = response_text
        mock_google = MagicMock()
        mock_google.genai = mock_genai
        return mock_google, mock_genai

    def test_primary_model_used_when_within_limits(self, tmp_path):
        from unittest.mock import patch
        from server_watchdog import llm

        limiter = GeminiRateLimiter(state_path=str(tmp_path / "s.json"))
        mock_google, mock_genai = self._make_google_mock("primary ok")

        with patch.dict("sys.modules", {"google": mock_google, "google.genai": mock_genai}):
            result = llm._call_gemini("key", MODEL, "prompt", limiter, FALLBACK)

        assert result == "primary ok"
        call_args = mock_genai.Client.return_value.models.generate_content.call_args
        assert call_args[1]["model"] == MODEL

    def test_fallback_used_when_primary_exhausted(self, tmp_path):
        from unittest.mock import patch
        from server_watchdog import llm

        limiter = GeminiRateLimiter(state_path=str(tmp_path / "s.json"))
        # Exhaust the primary model's RPD
        for _ in range(REQUESTS_PER_DAY):
            limiter.record(MODEL, 10)

        mock_google, mock_genai = self._make_google_mock("fallback ok")

        with patch.dict("sys.modules", {"google": mock_google, "google.genai": mock_genai}):
            result = llm._call_gemini("key", MODEL, "prompt", limiter, FALLBACK)

        assert result == "fallback ok"
        call_args = mock_genai.Client.return_value.models.generate_content.call_args
        assert call_args[1]["model"] == FALLBACK

    def test_both_exhausted_returns_error_message(self, tmp_path):
        from server_watchdog import llm

        limiter = GeminiRateLimiter(state_path=str(tmp_path / "s.json"))
        for _ in range(REQUESTS_PER_DAY):
            limiter.record(MODEL, 10)
            limiter.record(FALLBACK, 10)

        result = llm._call_gemini("key", MODEL, "prompt", limiter, FALLBACK)

        assert "rate limits exceeded" in result.lower()
        assert "LLM" in result

    def test_no_fallback_configured_returns_error_when_exhausted(self, tmp_path):
        from server_watchdog import llm

        limiter = GeminiRateLimiter(state_path=str(tmp_path / "s.json"))
        for _ in range(REQUESTS_PER_DAY):
            limiter.record(MODEL, 10)

        result = llm._call_gemini("key", MODEL, "prompt", limiter, None)

        assert "rate limits exceeded" in result.lower()

    def test_fallback_triggers_warning_log(self, tmp_path, caplog):
        import logging
        from unittest.mock import patch
        from server_watchdog import llm

        limiter = GeminiRateLimiter(state_path=str(tmp_path / "s.json"))
        for _ in range(REQUESTS_PER_DAY):
            limiter.record(MODEL, 10)

        mock_google, mock_genai = self._make_google_mock("ok")

        with caplog.at_level(logging.WARNING, logger="server_watchdog.llm"):
            with patch.dict("sys.modules", {"google": mock_google, "google.genai": mock_genai}):
                llm._call_gemini("key", MODEL, "prompt", limiter, FALLBACK)

        assert any("reduced intelligence" in r.message for r in caplog.records)

    def test_analyse_avc_denials_passes_fallback(self, tmp_path):
        from unittest.mock import patch
        from server_watchdog.config import Config
        from server_watchdog.llm import analyse_avc_denials

        cfg = Config(config_path="/tmp/does_not_exist_watchdog.ini")
        cfg._parser.set("llm", "api_key", "fake")
        cfg._parser.set("llm", "model", MODEL)
        cfg._parser.set("llm", "fallback_model", FALLBACK)
        cfg._parser.set("llm", "rate_limit_state_path", str(tmp_path / "s.json"))

        with patch("server_watchdog.llm._call_gemini", return_value="analysis") as mock_call:
            result = analyse_avc_denials(cfg, ["avc: denied { read }"])

        assert result == "analysis"
        _, _, _, limiter_arg, fallback_arg = mock_call.call_args[0]
        assert fallback_arg == FALLBACK

    def test_analyse_maintenance_report_passes_fallback(self, tmp_path):
        from unittest.mock import patch
        from server_watchdog.config import Config
        from server_watchdog.llm import analyse_maintenance_report

        cfg = Config(config_path="/tmp/does_not_exist_watchdog.ini")
        cfg._parser.set("llm", "api_key", "fake")
        cfg._parser.set("llm", "model", MODEL)
        cfg._parser.set("llm", "fallback_model", FALLBACK)
        cfg._parser.set("llm", "rate_limit_state_path", str(tmp_path / "s.json"))

        raw = {
            "hostname": "host", "timestamp": "2026-01-01", "server_context": "test",
            "uid_map": {}, "packages": {"updates": [], "error": None},
            "services": {"failed": [], "logs": {}, "error": None},
            "storage": {"filesystems": [], "nfs_filesystems": [], "all_output": "", "error": None},
            "journal_errors": {"errors": [], "error": None},
            "coredumps": {"dumps": [], "error": None},
            "threshold": 80, "lookback": 30, "coredump_age": 45,
        }

        with patch("server_watchdog.llm._call_gemini", return_value="report") as mock_call:
            result = analyse_maintenance_report(cfg, raw)

        assert result == "report"
        _, _, _, limiter_arg, fallback_arg = mock_call.call_args[0]
        assert fallback_arg == FALLBACK
