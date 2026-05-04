"""Rate limit tracking for Gemini API calls.

Free-tier limits (applied per model independently):
  - 5 requests per minute (RPM)
  - 250,000 tokens per minute (TPM)
  - 20 requests per day (RPD)

State is persisted to a JSON file so that limits are respected across
restarts of the watchdog processes.
"""

import json
import logging
import os
import time
from threading import Lock

logger = logging.getLogger(__name__)

# Gemini free-tier limits
REQUESTS_PER_MINUTE = 5
TOKENS_PER_MINUTE = 250_000
REQUESTS_PER_DAY = 20

# Conservative chars-per-token approximation (errs on the side of caution)
_CHARS_PER_TOKEN = 3

DEFAULT_STATE_PATH = "/var/lib/server-watchdog/rate_limit_state.json"

# Module-level lock guards in-process concurrent access.  Cross-process
# safety relies on the infrequent call pattern of the watchdog services.
_lock = Lock()


def estimate_tokens(text: str) -> int:
    """Return a conservative token estimate for *text*.

    Uses a rough 3-characters-per-token heuristic which errs on the side of
    over-counting to stay safely within the TPM budget.
    """
    return max(1, len(text) // _CHARS_PER_TOKEN)


class GeminiRateLimiter:
    """Tracks Gemini API usage and enforces free-tier rate limits.

    Limits are tracked **per model** so that a primary model and a fallback
    model each have their own independent quota.

    Parameters
    ----------
    state_path:
        Path to the JSON file used to persist state across process restarts.
    """

    def __init__(self, state_path: str = DEFAULT_STATE_PATH):
        self._path = state_path
        self._state: dict = self._load()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            if os.path.exists(self._path):
                with open(self._path) as fh:
                    return json.load(fh)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "Could not load rate-limit state from %s: %s", self._path, exc
            )
        return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w") as fh:
                json.dump(self._state, fh)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "Could not save rate-limit state to %s: %s", self._path, exc
            )

    # ------------------------------------------------------------------
    # Internal state management
    # ------------------------------------------------------------------

    def _model_state(self, model: str) -> dict:
        if model not in self._state:
            self._state[model] = {"minute_calls": [], "day_calls": []}
        return self._state[model]

    def _prune(self, model: str) -> None:
        """Discard entries older than the relevant window."""
        now = time.time()
        ms = self._model_state(model)
        ms["minute_calls"] = [
            e for e in ms["minute_calls"] if now - e["t"] < 60
        ]
        ms["day_calls"] = [t for t in ms["day_calls"] if now - t < 86400]

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def within_limits(self, model: str, token_estimate: int) -> bool:
        """Return ``True`` if a call to *model* using *token_estimate* tokens is allowed.

        Parameters
        ----------
        model:
            Gemini model name (e.g. ``"gemini-2.5-flash"``).
        token_estimate:
            Estimated number of tokens for the request (see :func:`estimate_tokens`).
        """
        with _lock:
            self._prune(model)
            ms = self._model_state(model)

            rpm = len(ms["minute_calls"])
            if rpm >= REQUESTS_PER_MINUTE:
                logger.debug(
                    "Rate limit: RPM exceeded for %s (%d/%d in last minute)",
                    model, rpm, REQUESTS_PER_MINUTE,
                )
                return False

            rpd = len(ms["day_calls"])
            if rpd >= REQUESTS_PER_DAY:
                logger.debug(
                    "Rate limit: RPD exceeded for %s (%d/%d today)",
                    model, rpd, REQUESTS_PER_DAY,
                )
                return False

            minute_tokens = sum(e["tokens"] for e in ms["minute_calls"])
            if minute_tokens + token_estimate > TOKENS_PER_MINUTE:
                logger.debug(
                    "Rate limit: TPM exceeded for %s (%d + %d > %d)",
                    model, minute_tokens, token_estimate, TOKENS_PER_MINUTE,
                )
                return False

            return True

    def record(self, model: str, token_estimate: int) -> None:
        """Record a completed API call for *model* and persist state."""
        with _lock:
            now = time.time()
            ms = self._model_state(model)
            ms["minute_calls"].append({"t": now, "tokens": token_estimate})
            ms["day_calls"].append(now)
            self._save()
