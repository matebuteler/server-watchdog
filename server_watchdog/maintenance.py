"""Monthly maintenance checks for server-watchdog."""

import logging
import subprocess
from datetime import datetime, timedelta

from .utils import escape_html, get_hostname, get_uid_map, markdown_to_html

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


def get_service_logs(unit_name, lines=50):
    """Return recent journal log lines for a systemd service unit.

    Parameters
    ----------
    unit_name:
        The systemd unit name (e.g. ``"myapp.service"``).
    lines:
        Maximum number of log lines to return.

    Returns
    -------
    str
        Log output, or an error string if journalctl is unavailable.
    """
    try:
        result = subprocess.run(
            [
                "journalctl", "-u", unit_name,
                "--no-pager", f"-n{lines}", "--output=short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip()
    except FileNotFoundError:
        return "(journalctl not found)"
    except subprocess.TimeoutExpired:
        return "(journalctl timed out)"


def check_storage(threshold=80):
    """Return a dict with filesystems at or above *threshold* percent used.

    NFS filesystems (fstype ``nfs`` / ``nfs4``) are reported in a separate
    list (``nfs_filesystems``) so callers can treat them as lower priority.

    Parameters
    ----------
    threshold:
        Integer percentage (0-100).  Filesystems below this are omitted.

    Returns
    -------
    dict
        ``{'filesystems': [...], 'nfs_filesystems': [...],
        'all_output': str, 'threshold': int, 'error': None|str}``
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
        concerning = []
        nfs_concerning = []
        for line in lines[1:]:
            parts = line.split()
            if not parts:
                continue
            fstype = parts[1] if len(parts) >= 2 else ""
            pct_str = parts[5] if len(parts) >= 6 else ""
            try:
                pct = int(pct_str.rstrip("%"))
            except ValueError:
                continue
            if pct >= threshold:
                if fstype.lower().startswith("nfs"):
                    nfs_concerning.append(line.strip())
                else:
                    concerning.append(line.strip())
        return {
            "filesystems": concerning,
            "nfs_filesystems": nfs_concerning,
            "all_output": result.stdout.strip(),
            "threshold": threshold,
            "error": None,
        }
    except FileNotFoundError:
        return {
            "filesystems": [], "nfs_filesystems": [],
            "all_output": "", "threshold": threshold,
            "error": "df not found",
        }
    except subprocess.TimeoutExpired:
        return {
            "filesystems": [], "nfs_filesystems": [],
            "all_output": "", "threshold": threshold,
            "error": "df timed out",
        }


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


def check_coredumps(max_age_days=45):
    """Return a dict with coredumps from the last *max_age_days* days.

    Uses ``coredumpctl list --no-pager --no-legend`` and filters entries older
    than *max_age_days*.  Entries whose timestamp cannot be parsed are included
    conservatively.

    Parameters
    ----------
    max_age_days:
        Coredumps older than this many days are excluded.

    Returns
    -------
    dict
        ``{'dumps': [...], 'error': None|str}``
    """
    cutoff = datetime.now() - timedelta(days=max_age_days)
    try:
        result = subprocess.run(
            ["coredumpctl", "list", "--no-pager", "--no-legend"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Exit code 1 just means no coredumps found on some distros
        if result.returncode not in (0, 1):
            return {
                "dumps": [],
                "error": result.stderr.strip() or "coredumpctl failed",
            }

        dumps = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            # coredumpctl output: "DayName YYYY-MM-DD HH:MM:SS TZ PID UID ..."
            if len(parts) < 5:
                dumps.append(line)  # include unparseable lines conservatively
                continue
            try:
                ts = datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S")
                if ts < cutoff:
                    continue
            except (ValueError, IndexError):
                pass  # include if date cannot be parsed
            dumps.append(line)

        return {"dumps": dumps, "error": None}
    except FileNotFoundError:
        return {"dumps": [], "error": "coredumpctl not found"}
    except subprocess.TimeoutExpired:
        return {"dumps": [], "error": "coredumpctl timed out"}


# ---------------------------------------------------------------------------
# Report building
# ---------------------------------------------------------------------------

def _extract_unit_name(svc_line):
    """Extract the systemd unit name from a ``systemctl list-units`` output line.

    Handles lines with and without a leading ``●`` / ``✗`` symbol.
    """
    for word in svc_line.split():
        if word not in ("●", "✗", "×", "○"):
            return word
    return ""


def build_report(config):
    """Run all enabled maintenance checks and return (text, html) reports.

    When an LLM API key is configured the raw data is passed to
    :func:`server_watchdog.llm.analyse_maintenance_report` and the LLM output
    is used as the report body.  If the LLM is unavailable or the key is not
    set, a static plain-text/HTML report is generated instead.

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
    coredump_age = config.getint("maintenance", "coredump_age_days", fallback=45)

    # ── Collect raw data ──────────────────────────────────────────────────
    pkg_data = None
    svc_data = None
    sto_data = None

    if config.getboolean("maintenance", "check_packages", fallback=True):
        pkg_data = check_packages()

    if config.getboolean("maintenance", "check_services", fallback=True):
        svc_data = check_failed_services()
        svc_data["logs"] = {}
        for svc_line in svc_data.get("failed", []):
            unit = _extract_unit_name(svc_line)
            if unit:
                svc_data["logs"][unit] = get_service_logs(unit)

    if config.getboolean("maintenance", "check_storage", fallback=True):
        sto_data = check_storage(threshold=threshold)

    jnl_data = check_journal_errors(lookback_days=lookback)
    core_data = check_coredumps(max_age_days=coredump_age)

    raw = {
        "hostname": hostname,
        "timestamp": now,
        "server_context": config.get("server", "context", fallback="Linux server"),
        "uid_map": get_uid_map(),
        "packages": pkg_data,
        "services": svc_data,
        "storage": sto_data,
        "journal_errors": jnl_data,
        "coredumps": core_data,
        "threshold": threshold,
        "lookback": lookback,
        "coredump_age": coredump_age,
    }

    # ── LLM analysis (optional) ───────────────────────────────────────────
    from .llm import analyse_maintenance_report  # pylint: disable=import-outside-toplevel
    api_key = config.get("llm", "api_key", fallback="")
    if api_key:
        provider = config.get("llm", "provider", fallback="gemini")
        model = config.get("llm", "model", fallback="gemini-1.5-pro")
        print(
            f"  LLM maintenance analysis: provider={provider}, model={model}, "
            f"key=<configured>",
            flush=True,
        )
        print("  Requesting LLM maintenance analysis…", flush=True)
        llm_text = analyse_maintenance_report(config, raw)
        if llm_text and not llm_text.startswith("(LLM"):
            print("  ✓ LLM maintenance analysis complete.", flush=True)
            html_body = (
                f"<h1>Server Maintenance Report</h1>"
                f"<p><b>Host:</b> {escape_html(hostname)}&nbsp;|&nbsp;"
                f"<b>Date:</b> {escape_html(now)}</p>"
                f"<div class='llm-report'>{markdown_to_html(llm_text)}</div>"
            )
            return llm_text, _wrap_html(html_body)
        print(
            f"  ✗ LLM maintenance analysis failed ({llm_text}); "
            f"falling back to static report.",
            flush=True,
        )
    else:
        print(
            "  LLM maintenance analysis: no api_key configured – using static report.",
            flush=True,
        )

    # ── Static fallback ───────────────────────────────────────────────────
    return _build_static_report(raw)


def _build_static_report(raw):
    """Build a static plain-text / HTML report from *raw* data (no LLM)."""
    hostname = raw["hostname"]
    now = raw["timestamp"]
    threshold = raw["threshold"]
    lookback = raw["lookback"]
    coredump_age = raw["coredump_age"]

    sections_text = []
    sections_html = []

    sections_text.append(f"Server Maintenance Report\nHost: {hostname}\nDate: {now}\n")
    sections_html.append(
        f"<h1>Server Maintenance Report</h1>"
        f"<p><b>Host:</b> {hostname}<br><b>Date:</b> {now}</p>"
    )

    # --- Packages ---
    pkg = raw.get("packages")
    if pkg is not None:
        if pkg["error"]:
            t = f"[PACKAGES]\nError: {pkg['error']}\n"
            h = f"<h2>Package Updates</h2><p class='error'>Error: {pkg['error']}</p>"
        elif pkg["updates"]:
            count = len(pkg["updates"])
            listing = "\n".join(pkg["updates"])
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
    svc = raw.get("services")
    if svc is not None:
        if svc["error"]:
            t = f"[SERVICES]\nError: {svc['error']}\n"
            h = f"<h2>Failed Services</h2><p class='error'>Error: {svc['error']}</p>"
        elif svc["failed"]:
            listing = "\n".join(svc["failed"])
            t = f"[SERVICES]\n{len(svc['failed'])} failed unit(s):\n{listing}\n"
            h = (
                f"<h2>Failed Services</h2>"
                f"<p>⚠️ {len(svc['failed'])} failed unit(s):</p>"
                f"<pre>{escape_html(listing)}</pre>"
            )
        else:
            t = "[SERVICES]\nNo failed services.\n"
            h = "<h2>Failed Services</h2><p>✅ No failed services.</p>"
        sections_text.append(t)
        sections_html.append(h)

    # --- Storage ---
    sto = raw.get("storage")
    if sto is not None:
        if sto["error"]:
            t = f"[STORAGE]\nError: {sto['error']}\n"
            h = f"<h2>Storage Usage</h2><p class='error'>Error: {sto['error']}</p>"
        else:
            local_fs = sto.get("filesystems", [])
            nfs_fs = sto.get("nfs_filesystems", [])
            all_out = sto.get("all_output", "")
            all_concerning = local_fs + nfs_fs
            if all_concerning:
                listing = "\n".join(local_fs)
                nfs_listing = "\n".join(nfs_fs)
                t_parts = [
                    f"[STORAGE]\n{len(all_concerning)} filesystem(s) above {threshold}%:"
                ]
                if local_fs:
                    t_parts.append(listing)
                if nfs_fs:
                    t_parts.append(f"NFS mounts (lower priority):\n{nfs_listing}")
                t_parts.append(f"\nFull disk usage:\n{all_out}")
                t = "\n".join(t_parts) + "\n"

                h_parts = [
                    f"<h2>Storage Usage</h2>"
                    f"<p>⚠️ {len(all_concerning)} filesystem(s) above {threshold}%:</p>"
                ]
                if local_fs:
                    h_parts.append(f"<pre>{escape_html(listing)}</pre>")
                if nfs_fs:
                    h_parts.append(
                        f"<p><em>NFS mounts (lower priority):</em></p>"
                        f"<pre>{escape_html(nfs_listing)}</pre>"
                    )
                h_parts.append(
                    f"<details><summary>Full disk usage</summary>"
                    f"<pre>{escape_html(all_out)}</pre></details>"
                )
                h = "".join(h_parts)
            else:
                t = (
                    f"[STORAGE]\nAll filesystems below {threshold}% usage.\n\n"
                    f"{all_out}\n"
                )
                h = (
                    f"<h2>Storage Usage</h2>"
                    f"<p>✅ All filesystems below {threshold}% usage.</p>"
                    f"<details><summary>Full disk usage</summary>"
                    f"<pre>{escape_html(all_out)}</pre></details>"
                )
        sections_text.append(t)
        sections_html.append(h)

    # --- Journal errors ---
    jnl = raw["journal_errors"]
    if jnl["error"]:
        t = f"[JOURNAL ERRORS]\nError: {jnl['error']}\n"
        h = (
            f"<h2>Journal Errors (last {lookback} days)</h2>"
            f"<p class='error'>Error: {jnl['error']}</p>"
        )
    elif jnl["errors"]:
        listing = "\n".join(jnl["errors"])
        t = (
            f"[JOURNAL ERRORS (last {lookback} days)]\n"
            f"{len(jnl['errors'])} error/critical message(s):\n{listing}\n"
        )
        h = (
            f"<h2>Journal Errors (last {lookback} days)</h2>"
            f"<p>⚠️ {len(jnl['errors'])} error/critical message(s):</p>"
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

    # --- Coredumps ---
    core = raw["coredumps"]
    if core.get("error") and "not found" not in core["error"]:
        t = f"[COREDUMPS]\nError: {core['error']}\n"
        h = f"<h2>Coredumps</h2><p class='error'>Error: {core['error']}</p>"
        sections_text.append(t)
        sections_html.append(h)
    elif core.get("dumps"):
        listing = "\n".join(core["dumps"])
        t = (
            f"[COREDUMPS (last {coredump_age} days)]\n"
            f"{len(core['dumps'])} coredump(s):\n{listing}\n"
        )
        h = (
            f"<h2>Coredumps (last {coredump_age} days)</h2>"
            f"<p>⚠️ {len(core['dumps'])} coredump(s) found:</p>"
            f"<pre>{escape_html(listing)}</pre>"
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
  .llm-report {{ font-family: sans-serif; line-height: 1.6; }}
  details summary {{ cursor: pointer; color: #0066cc; }}
</style>
</head>
<body>
{body}
</body>
</html>"""
