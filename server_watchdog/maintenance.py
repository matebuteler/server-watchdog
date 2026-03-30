"""Monthly maintenance checks for server-watchdog."""

import logging
import subprocess
from datetime import datetime

from .utils import escape_html, get_hostname

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_packages():
    """Return a dict with available package updates.

    Returns
    -------
    dict
        ``{'updates': [...], 'error': None|str}``
    """
    try:
        result = subprocess.run(
            ["dnf", "check-update", "--quiet"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        # exit code 100 means updates available; 0 means up-to-date
        if result.returncode not in (0, 100):
            return {"updates": [], "error": result.stderr.strip() or "dnf check-update failed"}

        lines = [
            line for line in result.stdout.splitlines()
            if line.strip() and not line.startswith("Last metadata")
        ]
        return {"updates": lines, "error": None}
    except FileNotFoundError:
        return {"updates": [], "error": "dnf not found"}
    except subprocess.TimeoutExpired:
        return {"updates": [], "error": "dnf check-update timed out"}


def check_failed_services():
    """Return a dict with failed systemd units.

    Returns
    -------
    dict
        ``{'failed': [...], 'error': None|str}``
    """
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--state=failed", "--no-legend", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        failed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return {"failed": failed, "error": None}
    except FileNotFoundError:
        return {"failed": [], "error": "systemctl not found"}
    except subprocess.TimeoutExpired:
        return {"failed": [], "error": "systemctl timed out"}


def check_storage(threshold=80):
    """Return a dict with filesystems at or above *threshold* percent used.

    Parameters
    ----------
    threshold:
        Integer percentage (0-100).  Filesystems below this are omitted.

    Returns
    -------
    dict
        ``{'filesystems': [...], 'threshold': int, 'error': None|str}``
    """
    try:
        result = subprocess.run(
            ["df", "--output=source,fstype,size,used,avail,pcent,target", "-h", "-x", "tmpfs",
             "-x", "devtmpfs"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        lines = result.stdout.splitlines()
        header = lines[0] if lines else ""
        concerning = []
        for line in lines[1:]:
            parts = line.split()
            if not parts:
                continue
            pct_str = parts[5] if len(parts) >= 6 else ""
            try:
                pct = int(pct_str.rstrip("%"))
            except ValueError:
                continue
            if pct >= threshold:
                concerning.append(line.strip())
        return {
            "filesystems": concerning,
            "all_output": result.stdout.strip(),
            "threshold": threshold,
            "error": None,
        }
    except FileNotFoundError:
        return {"filesystems": [], "all_output": "", "threshold": threshold,
                "error": "df not found"}
    except subprocess.TimeoutExpired:
        return {"filesystems": [], "all_output": "", "threshold": threshold,
                "error": "df timed out"}


def check_journal_errors(lookback_days=30):
    """Return a dict with error/critical messages from the systemd journal.

    Parameters
    ----------
    lookback_days:
        How many calendar days back to search.

    Returns
    -------
    dict
        ``{'errors': [...], 'error': None|str}``
    """
    since = f"{lookback_days} days ago"
    try:
        result = subprocess.run(
            [
                "journalctl",
                "--priority=err",
                f"--since={since}",
                "--no-pager",
                "--output=short-iso",
                "--lines=200",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        errors = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return {"errors": errors, "error": None}
    except FileNotFoundError:
        return {"errors": [], "error": "journalctl not found"}
    except subprocess.TimeoutExpired:
        return {"errors": [], "error": "journalctl timed out"}


# ---------------------------------------------------------------------------
# Report building
# ---------------------------------------------------------------------------

def build_report(config):
    """Run all enabled maintenance checks and return (text, html) reports.

    Parameters
    ----------
    config:
        A :class:`~server_watchdog.config.Config` instance.

    Returns
    -------
    tuple[str, str]
        ``(plain_text_report, html_report)``
    """
    hostname = get_hostname()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    threshold = config.getint("maintenance", "storage_threshold", fallback=80)
    lookback = config.getint("maintenance", "log_lookback_days", fallback=30)

    sections_text = []
    sections_html = []

    sections_text.append(f"Server Maintenance Report\nHost: {hostname}\nDate: {now}\n")
    sections_html.append(
        f"<h1>Server Maintenance Report</h1>"
        f"<p><b>Host:</b> {hostname}<br><b>Date:</b> {now}</p>"
    )

    # --- Packages ---
    if config.getboolean("maintenance", "check_packages", fallback=True):
        data = check_packages()
        if data["error"]:
            t = f"[PACKAGES]\nError: {data['error']}\n"
            h = f"<h2>Package Updates</h2><p class='error'>Error: {data['error']}</p>"
        elif data["updates"]:
            count = len(data["updates"])
            listing = "\n".join(data["updates"])
            t = f"[PACKAGES]\n{count} update(s) available:\n{listing}\n"
            h = (
                f"<h2>Package Updates</h2>"
                f"<p>{count} update(s) available:</p>"
                f"<pre>{escape_html(listing)}</pre>"
            )
        else:
            t = "[PACKAGES]\nAll packages are up to date.\n"
            h = "<h2>Package Updates</h2><p>✅ All packages are up to date.</p>"
        sections_text.append(t)
        sections_html.append(h)

    # --- Failed services ---
    if config.getboolean("maintenance", "check_services", fallback=True):
        data = check_failed_services()
        if data["error"]:
            t = f"[SERVICES]\nError: {data['error']}\n"
            h = f"<h2>Failed Services</h2><p class='error'>Error: {data['error']}</p>"
        elif data["failed"]:
            listing = "\n".join(data["failed"])
            t = f"[SERVICES]\n{len(data['failed'])} failed unit(s):\n{listing}\n"
            h = (
                f"<h2>Failed Services</h2>"
                f"<p>⚠️ {len(data['failed'])} failed unit(s):</p>"
                f"<pre>{escape_html(listing)}</pre>"
            )
        else:
            t = "[SERVICES]\nNo failed services.\n"
            h = "<h2>Failed Services</h2><p>✅ No failed services.</p>"
        sections_text.append(t)
        sections_html.append(h)

    # --- Storage ---
    if config.getboolean("maintenance", "check_storage", fallback=True):
        data = check_storage(threshold=threshold)
        if data["error"]:
            t = f"[STORAGE]\nError: {data['error']}\n"
            h = f"<h2>Storage Usage</h2><p class='error'>Error: {data['error']}</p>"
        elif data["filesystems"]:
            listing = "\n".join(data["filesystems"])
            t = (
                f"[STORAGE]\n{len(data['filesystems'])} filesystem(s) above {threshold}%:\n"
                f"{listing}\n\nFull disk usage:\n{data['all_output']}\n"
            )
            h = (
                f"<h2>Storage Usage</h2>"
                f"<p>⚠️ {len(data['filesystems'])} filesystem(s) above {threshold}%:</p>"
                f"<pre>{escape_html(listing)}</pre>"
                f"<details><summary>Full disk usage</summary>"
                f"<pre>{escape_html(data['all_output'])}</pre></details>"
            )
        else:
            t = f"[STORAGE]\nAll filesystems below {threshold}% usage.\n\n{data['all_output']}\n"
            h = (
                f"<h2>Storage Usage</h2>"
                f"<p>✅ All filesystems below {threshold}% usage.</p>"
                f"<details><summary>Full disk usage</summary>"
                f"<pre>{escape_html(data['all_output'])}</pre></details>"
            )
        sections_text.append(t)
        sections_html.append(h)

    # --- Journal errors ---
    data = check_journal_errors(lookback_days=lookback)
    if data["error"]:
        t = f"[JOURNAL ERRORS]\nError: {data['error']}\n"
        h = f"<h2>Journal Errors (last {lookback} days)</h2><p class='error'>Error: {data['error']}</p>"
    elif data["errors"]:
        listing = "\n".join(data["errors"])
        t = (
            f"[JOURNAL ERRORS (last {lookback} days)]\n"
            f"{len(data['errors'])} error/critical message(s):\n{listing}\n"
        )
        h = (
            f"<h2>Journal Errors (last {lookback} days)</h2>"
            f"<p>⚠️ {len(data['errors'])} error/critical message(s):</p>"
            f"<pre>{escape_html(listing)}</pre>"
        )
    else:
        t = f"[JOURNAL ERRORS]\nNo error/critical messages in the last {lookback} days.\n"
        h = (
            f"<h2>Journal Errors (last {lookback} days)</h2>"
            f"<p>✅ No error/critical messages found.</p>"
        )
    sections_text.append(t)
    sections_html.append(h)

    plain = "\n".join(sections_text)
    html = _wrap_html("\n".join(sections_html))
    return plain, html


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wrap_html(body):
    return f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: monospace; max-width: 900px; margin: 2em auto; }}
  h1 {{ color: #333; }}
  h2 {{ color: #555; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
  pre {{ background: #f4f4f4; padding: 1em; overflow-x: auto; white-space: pre-wrap; }}
  .error {{ color: red; }}
  details summary {{ cursor: pointer; color: #0066cc; }}
</style>
</head>
<body>
{body}
</body>
</html>"""
