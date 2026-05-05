"""Tests for server_watchdog.rate_limiter."""

import json
import os
import tempfile
import time
from unittest.mock import patch

import pytest

from server_watchdog.rate_limiter import (
    RateLimiter,
    MODEL_LIMITS,
    DEFAULT_FALLBACK_CHAIN,
    estimate_tokens,
)


def _temp_state_path(tmp_path):
    return str(tmp_path / "rate_state.json")


class TestEstimateTokens:
    def test_short_text(self):
        assert estimate_tokens("hello") >= 1

    def test_long_text(self):
        text = "x" * 4000
        assert estimate_tokens(text) == 1000

    def test_empty_text(self):
        assert estimate_tokens("") == 1


class TestRateLimiterBasic:
    def test_no_usage_returns_same_model(self, tmp_path):
        limiter = RateLimiter(state_path=_temp_state_path(tmp_path))
        model = limiter.check_and_wait(100, "gemini-3-flash-preview")
        assert model == "gemini-3-flash-preview"

    def test_record_usage_persists(self, tmp_path):
        path = _temp_state_path(tmp_path)
        limiter = RateLimiter(state_path=path)
        limiter.record_usage("gemini-3-flash-preview", 100, 50)

        # Verify state file was written
        assert os.path.exists(path)
        with open(path) as f:
            state = json.load(f)
        assert len(state["requests"]) == 1
        assert state["requests"][0]["model"] == "gemini-3-flash-preview"
        assert state["requests"][0]["tokens"] == 150

    def test_state_loads_across_instances(self, tmp_path):
        path = _temp_state_path(tmp_path)

        limiter1 = RateLimiter(state_path=path)
        limiter1.record_usage("gemini-3-flash-preview", 100, 50)

        limiter2 = RateLimiter(state_path=path)
        requests = limiter2._state.get("requests", [])
        assert len(requests) == 1


class TestRPMLimit:
    def test_rpm_triggers_fallback(self, tmp_path):
        path = _temp_state_path(tmp_path)
        limiter = RateLimiter(state_path=path)

        # Fill up gemini-3-flash-preview RPM (limit = 5)
        for _ in range(5):
            limiter.record_usage("gemini-3-flash-preview", 100, 50)

        model = limiter.check_and_wait(100, "gemini-3-flash-preview")
        assert model != "gemini-3-flash-preview"
        assert model in DEFAULT_FALLBACK_CHAIN

    def test_rpm_does_not_trigger_under_limit(self, tmp_path):
        path = _temp_state_path(tmp_path)
        limiter = RateLimiter(state_path=path)

        # Use 4 of 5 RPM
        for _ in range(4):
            limiter.record_usage("gemini-3-flash-preview", 100, 50)

        model = limiter.check_and_wait(100, "gemini-3-flash-preview")
        assert model == "gemini-3-flash-preview"


class TestRPDLimit:
    def test_rpd_triggers_fallback(self, tmp_path):
        path = _temp_state_path(tmp_path)
        limiter = RateLimiter(state_path=path)

        # Fill up gemini-3-flash-preview RPD (limit = 20)
        now = time.time()
        # Spread requests across the day (not all in same minute, to avoid RPM limit)
        for i in range(20):
            limiter._state["requests"].append({
                "ts": now - (i * 120),  # every 2 minutes
                "model": "gemini-3-flash-preview",
                "tokens": 100,
            })

        model = limiter.check_and_wait(100, "gemini-3-flash-preview")
        assert model != "gemini-3-flash-preview"


class TestCascadingFallback:
    def test_cascades_through_chain(self, tmp_path):
        """When primary and first fallback are exhausted, cascades to third."""
        path = _temp_state_path(tmp_path)
        limiter = RateLimiter(state_path=path)

        now = time.time()

        # Exhaust gemini-3-flash-preview RPM (5)
        for i in range(5):
            limiter._state["requests"].append({
                "ts": now - i, "model": "gemini-3-flash-preview", "tokens": 100,
            })

        # Exhaust gemma-4-31b-it RPM (15)
        for i in range(15):
            limiter._state["requests"].append({
                "ts": now - i, "model": "gemma-4-31b-it", "tokens": 100,
            })

        model = limiter.check_and_wait(100, "gemini-3-flash-preview")
        assert model == "gemini-3.1-flash-lite-preview"

    def test_unknown_model_no_limits(self, tmp_path):
        """Unknown models have no rate limits enforced."""
        path = _temp_state_path(tmp_path)
        limiter = RateLimiter(state_path=path)
        model = limiter.check_and_wait(100, "some-unknown-model")
        assert model == "some-unknown-model"


class TestNoFallbackMode:
    def test_no_fallback_waits(self, tmp_path):
        """In no-fallback mode, check_and_wait blocks instead of falling back."""
        path = _temp_state_path(tmp_path)
        limiter = RateLimiter(state_path=path, no_fallback=True)

        now = time.time()
        # Exhaust RPM — but set timestamps to ~59 seconds ago so the wait is short
        for i in range(5):
            limiter._state["requests"].append({
                "ts": now - 59.5 + (i * 0.01),
                "model": "gemini-3-flash-preview",
                "tokens": 100,
            })

        with patch("time.sleep") as mock_sleep:
            # Override _check_limits to clear after first sleep call
            original_check = limiter._check_limits
            call_count = [0]

            def side_effect_check(model, est_tokens):
                call_count[0] += 1
                if call_count[0] > 1:
                    return None  # cleared
                return original_check(model, est_tokens)

            limiter._check_limits = side_effect_check

            model = limiter.check_and_wait(100, "gemini-3-flash-preview")

        assert model == "gemini-3-flash-preview"
        assert mock_sleep.called


class TestPruning:
    def test_old_entries_pruned(self, tmp_path):
        path = _temp_state_path(tmp_path)
        limiter = RateLimiter(state_path=path)

        # Add an entry from 2 days ago
        limiter._state["requests"].append({
            "ts": time.time() - 200_000,
            "model": "gemini-3-flash-preview",
            "tokens": 100,
        })
        # Add a recent entry
        limiter._state["requests"].append({
            "ts": time.time(),
            "model": "gemini-3-flash-preview",
            "tokens": 100,
        })

        limiter._prune_old_entries()
        assert len(limiter._state["requests"]) == 1


class TestModelLimits:
    def test_all_chain_models_have_limits(self):
        """Every model in the default fallback chain must have defined limits."""
        for model in DEFAULT_FALLBACK_CHAIN:
            assert model in MODEL_LIMITS, f"Missing limits for {model}"

    def test_gemma_has_unlimited_tpm(self):
        assert MODEL_LIMITS["gemma-4-31b-it"]["tpm"] is None

    def test_chain_order(self):
        """Fallback chain should be in decreasing intelligence order."""
        assert DEFAULT_FALLBACK_CHAIN == [
            "gemini-3-flash-preview",
            "gemma-4-31b-it",
            "gemini-3.1-flash-lite-preview",
        ]
