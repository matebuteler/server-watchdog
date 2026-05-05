"""Gemini API rate limiting with cascading model fallback.

Tracks per-model request counts and token usage against free-tier limits.
When a model's limit is reached, the limiter either cascades to the next
model in the fallback chain (default) or waits until the rate window
expires (``--no-fallback`` mode).
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-model rate-limit registry (free-tier limits as of 2026-05)
# tpm=None means unlimited.
# ---------------------------------------------------------------------------
MODEL_LIMITS = {
    "gemini-3-flash-preview": {"rpm": 5, "tpm": 250_000, "rpd": 20},
    "gemma-4-31b-it": {"rpm": 15, "tpm": None, "rpd": 1_500},
    "gemini-3.1-flash-lite-preview": {"rpm": 15, "tpm": 250_000, "rpd": 500},
    # Legacy / lower-priority models kept for reference
    "gemini-2.5-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},
}

# Ordered by intelligence (AAII): 46 → 39 → 34
DEFAULT_FALLBACK_CHAIN = [
    "gemini-3-flash-preview",
    "gemma-4-31b-it",
    "gemini-3.1-flash-lite-preview",
]

# Rough characters-per-token ratio for estimation
_CHARS_PER_TOKEN = 4


def estimate_tokens(text):
    """Estimate the token count of *text* using a simple heuristic."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


class RateLimiter:
    """Track Gemini API usage and enforce per-model rate limits.

    Parameters
    ----------
    state_path:
        Filesystem path to the JSON state file.  If ``None``, tries
        ``/var/lib/server-watchdog/rate_state.json`` first, then falls
        back to ``~/.local/share/server-watchdog/rate_state.json``.
    no_fallback:
        When ``True``, never switch models — wait for the primary
        model's rate window to expire instead.
    fallback_chain:
        Ordered list of model codenames.  The first entry is the
        primary model; subsequent entries are fallbacks.
    """

    def __init__(self, state_path=None, no_fallback=False, fallback_chain=None):
        self.no_fallback = no_fallback
        self.fallback_chain = list(fallback_chain or DEFAULT_FALLBACK_CHAIN)
        self._state_path = state_path or self._default_state_path()
        self._state = self._load_state()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def check_and_wait(self, estimated_tokens, model):
        """Determine which model to use, respecting rate limits.

        If *model* is within limits, returns it unchanged.  Otherwise:

        - **fallback mode** (default): walks the fallback chain and
          returns the first model that has capacity.  If all models are
          exhausted, sleeps on the one with the shortest remaining window.
        - **no-fallback mode**: sleeps until *model*'s rate window opens.

        Parameters
        ----------
        estimated_tokens:
            Rough number of tokens the request is expected to consume
            (prompt + estimated response).
        model:
            The model codename the caller wants to use.

        Returns
        -------
        str
            The model codename to actually use.
        """
        if self.no_fallback:
            self._wait_if_needed(model, estimated_tokens)
            return model

        # Walk the fallback chain starting from the requested model
        chain = self._chain_from(model)
        for candidate in chain:
            violation = self._check_limits(candidate, estimated_tokens)
            if violation is None:
                if candidate != model:
                    limits = MODEL_LIMITS.get(candidate, {})
                    logger.warning(
                        "Rate limit reached for %s; falling back to %s "
                        "(lower intelligence). Limits: RPM=%s TPM=%s RPD=%s",
                        model, candidate,
                        limits.get("rpm"), limits.get("tpm", "unlimited"),
                        limits.get("rpd"),
                    )
                return candidate

        # All models exhausted — sleep on the one with shortest wait
        best_model, wait_secs = self._shortest_wait(chain, estimated_tokens)
        logger.warning(
            "All models rate-limited. Waiting %.1fs for %s…",
            wait_secs, best_model,
        )
        time.sleep(wait_secs)
        return best_model

    def record_usage(self, model, prompt_tokens, response_tokens):
        """Record a completed API call for rate-limit tracking.

        Parameters
        ----------
        model:
            The model codename that was used.
        prompt_tokens:
            Estimated token count of the prompt.
        response_tokens:
            Estimated token count of the response.
        """
        now = time.time()
        total_tokens = prompt_tokens + response_tokens
        self._state.setdefault("requests", []).append({
            "ts": now,
            "model": model,
            "tokens": total_tokens,
        })
        self._prune_old_entries()
        self._save_state()

    # ------------------------------------------------------------------
    # Limit checking
    # ------------------------------------------------------------------

    def _check_limits(self, model, estimated_tokens):
        """Return a violation string if *model* would exceed a limit, else None."""
        limits = MODEL_LIMITS.get(model)
        if limits is None:
            return None  # unknown model → no limits enforced

        now = time.time()
        minute_ago = now - 60
        day_ago = now - 86400
        model_requests = [
            r for r in self._state.get("requests", [])
            if r["model"] == model
        ]

        # RPM check
        rpm_count = sum(1 for r in model_requests if r["ts"] > minute_ago)
        if rpm_count >= limits["rpm"]:
            return "rpm"

        # TPM check
        if limits["tpm"] is not None:
            tpm_used = sum(
                r["tokens"] for r in model_requests if r["ts"] > minute_ago
            )
            if tpm_used + estimated_tokens > limits["tpm"]:
                return "tpm"

        # RPD check
        rpd_count = sum(1 for r in model_requests if r["ts"] > day_ago)
        if rpd_count >= limits["rpd"]:
            return "rpd"

        return None

    def _wait_if_needed(self, model, estimated_tokens):
        """Block until *model* is within all rate limits."""
        while True:
            violation = self._check_limits(model, estimated_tokens)
            if violation is None:
                return
            wait = self._time_until_clear(model, violation)
            logger.info(
                "Rate limit (%s) hit for %s; waiting %.1fs…",
                violation, model, wait,
            )
            time.sleep(wait)

    def _time_until_clear(self, model, violation_type):
        """Estimate seconds until *violation_type* clears for *model*."""
        now = time.time()
        model_requests = [
            r for r in self._state.get("requests", [])
            if r["model"] == model
        ]

        if violation_type in ("rpm", "tpm"):
            # Wait until the oldest request within the last minute expires
            minute_ago = now - 60
            recent = sorted(
                (r["ts"] for r in model_requests if r["ts"] > minute_ago)
            )
            if recent:
                return max(0.5, (recent[0] + 60) - now + 0.5)
            return 1.0

        if violation_type == "rpd":
            # Wait until the oldest request within the last day expires
            day_ago = now - 86400
            recent = sorted(
                (r["ts"] for r in model_requests if r["ts"] > day_ago)
            )
            if recent:
                return max(0.5, (recent[0] + 86400) - now + 0.5)
            return 1.0

        return 1.0

    def _shortest_wait(self, chain, estimated_tokens):
        """Return (model, wait_seconds) for the model that clears soonest."""
        best_model = chain[0]
        best_wait = float("inf")
        for model in chain:
            violation = self._check_limits(model, estimated_tokens)
            if violation is None:
                return model, 0
            wait = self._time_until_clear(model, violation)
            if wait < best_wait:
                best_wait = wait
                best_model = model
        return best_model, best_wait

    # ------------------------------------------------------------------
    # Fallback chain helpers
    # ------------------------------------------------------------------

    def _chain_from(self, model):
        """Return the fallback chain starting from *model*.

        If *model* is in the chain, returns from that position onward.
        If not, prepends *model* to the full chain.
        """
        if model in self.fallback_chain:
            idx = self.fallback_chain.index(model)
            return self.fallback_chain[idx:]
        return [model] + self.fallback_chain

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _default_state_path():
        """Pick a writable location for the state file."""
        primary = "/var/lib/server-watchdog/rate_state.json"
        primary_dir = os.path.dirname(primary)
        try:
            os.makedirs(primary_dir, exist_ok=True)
            # Test writability
            test_path = os.path.join(primary_dir, ".write_test")
            with open(test_path, "w") as f:
                f.write("")
            os.unlink(test_path)
            return primary
        except OSError:
            pass
        fallback_dir = os.path.expanduser(
            "~/.local/share/server-watchdog"
        )
        os.makedirs(fallback_dir, exist_ok=True)
        return os.path.join(fallback_dir, "rate_state.json")

    def _load_state(self):
        """Load state from disk, or return empty state."""
        if os.path.exists(self._state_path):
            try:
                with open(self._state_path, encoding="utf-8") as fh:
                    state = json.load(fh)
                    self._prune_old_entries(state)
                    return state
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load rate state from %s: %s",
                               self._state_path, exc)
        return {"requests": []}

    def _save_state(self):
        """Persist the current state to disk."""
        try:
            state_dir = os.path.dirname(self._state_path)
            os.makedirs(state_dir, exist_ok=True)
            with open(self._state_path, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, indent=2)
        except OSError as exc:
            logger.warning("Could not save rate state to %s: %s",
                           self._state_path, exc)

    def _prune_old_entries(self, state=None):
        """Remove request records older than 24 hours."""
        if state is None:
            state = self._state
        cutoff = time.time() - 86400
        state["requests"] = [
            r for r in state.get("requests", [])
            if r.get("ts", 0) > cutoff
        ]
