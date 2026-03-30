"""Shared logging setup for server-watchdog entry points."""

import logging
import os
import sys


def setup_logging(config):
    """Configure root logger from *config*.

    If the configured log file directory is not writable (e.g. running as a
    normal user during development), falls back to stdout.
    """
    level_name = config.get("logging", "level", fallback="INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_file = config.get("logging", "log_file", fallback="")
    handlers = []

    if log_file:
        log_dir = os.path.dirname(log_file)
        try:
            os.makedirs(log_dir, exist_ok=True)
            handlers.append(logging.FileHandler(log_file))
        except OSError:
            pass  # fall through to stdout

    if not handlers:
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
    )
