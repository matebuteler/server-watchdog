"""Shared utility helpers for server-watchdog."""

import subprocess


def get_hostname():
    """Return the fully-qualified domain name of the current host."""
    try:
        result = subprocess.run(
            ["hostname", "-f"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:  # pylint: disable=broad-except
        return "unknown"


def escape_html(text):
    """Escape HTML special characters in *text*."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
