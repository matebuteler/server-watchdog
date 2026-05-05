"""Real-time SELinux/AppArmor denial monitor daemon for server-watchdog.

Architecture
------------
- Follow the systemd journal for messages containing ``avc: denied``
  (SELinux) or ``apparmor="DENIED"`` (AppArmor).
- Batch incoming denials for up to *batch_interval* seconds after the first
  one arrives, then:
    1. Call the configured LLM to produce a human-readable analysis.
    2. Send an email with the raw denials and the analysis.
- Repeat indefinitely.

The daemon is designed to run under systemd (``server-watchdog-avc.service``).
"""

import json
import logging
import signal
import subprocess
import threading
from datetime import datetime

from .email_sender import send_email
from .llm import analyse_avc_denials
from .utils import detect_mac_system, escape_html, get_hostname, markdown_to_html

logger = logging.getLogger(__name__)

# How many AVC lines to include in the email at most (prevents huge emails)
MAX_LINES_IN_EMAIL = 500


class AVCMonitor:
    """Watch the journal for AVC denials and alert via email."""

    def __init__(self, config):
        self._config = config
        self._batch_interval = config.getint("avc_monitor", "batch_interval", fallback=60)
        self._mac_system = detect_mac_system()
        self._running = False
        self._pending = []
        self._timer = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self):
        """Start the monitor loop.  Blocks until stopped by a signal."""
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        logger.info(
            "AVC monitor started (batch_interval=%ds).", self._batch_interval
        )

        for message in self._follow_journal():
            if not self._running:
                break
            if _is_mac_denial(message):
                self._enqueue(message)

        logger.info("AVC monitor stopped.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_signal(self, signum, _frame):
        logger.info("Received signal %s, stopping AVC monitor.", signum)
        self._running = False

    def _follow_journal(self):
        """Yield raw message strings from journalctl in real time.

        Uses ``journalctl -f --output=json -n 0`` and filters for AVC messages
        in Python so we don't depend on --grep (unavailable on RHEL8 systemd).
        """
        cmd = [
            "journalctl",
            "-f",
            "--output=json",
            "-n", "0",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            for raw_line in proc.stdout:
                if not self._running:
                    break
                try:
                    entry = json.loads(raw_line.decode("utf-8", errors="replace"))
                    yield entry.get("MESSAGE", "")
                except json.JSONDecodeError:
                    continue
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Journal follow error: %s", exc)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _enqueue(self, message):
        """Add an AVC message to the pending buffer and start the batch timer."""
        with self._lock:
            self._pending.append(message)
            if self._timer is None:
                logger.debug(
                    "First AVC denial in batch; starting %ds timer.", self._batch_interval
                )
                self._timer = threading.Timer(self._batch_interval, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self):
        """Process and send the accumulated batch of AVC denials."""
        with self._lock:
            denials = list(self._pending)
            self._pending.clear()
            self._timer = None

        if not denials:
            return

        logger.info("Flushing batch of %d AVC denial(s).", len(denials))

        try:
            analysis = analyse_avc_denials(
                self._config, denials, mac_system=self._mac_system
            )
            self._send_alert(denials, analysis)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to process AVC batch: %s", exc)

    def _send_alert(self, denials, analysis):
        """Compose and send the AVC alert email."""
        hostname = get_hostname()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        count = len(denials)
        subject = f"SELinux AVC Alert on {hostname}: {count} denial(s)"

        # Plain text
        raw_block = "\n".join(denials[:MAX_LINES_IN_EMAIL])
        if len(denials) > MAX_LINES_IN_EMAIL:
            raw_block += f"\n... ({len(denials) - MAX_LINES_IN_EMAIL} more lines omitted)"

        plain = (
            f"SELinux AVC Denial Alert\n"
            f"Host: {hostname}\n"
            f"Time: {now}\n"
            f"Count: {count}\n"
            f"\n--- RAW DENIALS ---\n{raw_block}\n"
            f"\n--- LLM ANALYSIS ---\n{analysis}\n"
        )

        # HTML
        html = _build_alert_html(hostname, now, count, raw_block, analysis)

        send_email(self._config, subject, plain, html)
        logger.info("AVC alert email sent (%d denial(s)).", count)


# ---------------------------------------------------------------------------
# Snapshot helper
# ---------------------------------------------------------------------------

def read_current_avc_denials(config):
    """Return a list of recent AVC denial messages from the systemd journal.

    Unlike the daemon's :meth:`AVCMonitor._follow_journal`, this is a
    one-shot read of *existing* journal entries (no ``-f``).

    Parameters
    ----------
    config:
        A :class:`~server_watchdog.config.Config` instance.  The
        ``[avc_monitor] avc_lookback_days`` key controls how many days of
        journal history to scan (default: 7).

    Returns
    -------
    list[str]
        Each element is the raw ``MESSAGE`` field of one AVC denial entry.
    """
    lookback = config.getint("avc_monitor", "avc_lookback_days", fallback=7)
    since = f"{lookback} days ago"
    cmd = [
        "journalctl",
        "--output=json",
        "--no-pager",
        f"--since={since}",
    ]
    denials = []
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,
        )
        for raw_line in proc.stdout.splitlines():
            try:
                entry = json.loads(raw_line.decode("utf-8", errors="replace"))
                msg = entry.get("MESSAGE", "")
                if _is_mac_denial(msg):
                    denials.append(msg)
            except (json.JSONDecodeError, AttributeError):
                continue
    except FileNotFoundError:
        logger.error("journalctl not found; cannot read AVC denials.")
    except subprocess.TimeoutExpired:
        logger.error("journalctl timed out while reading AVC denials.")
    return denials


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _is_mac_denial(message):
    """Return ``True`` if *message* is a SELinux AVC or AppArmor denial."""
    lower = message.lower()
    return "avc: denied" in lower or 'apparmor="denied"' in lower


def main(config):
    """Start the AVC monitor.  Intended to be called from the entry-point script."""
    monitor = AVCMonitor(config)
    monitor.run()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_alert_html(hostname, now, count, raw_block, analysis):
    """Return an HTML email body for an AVC alert."""
    # Convert Markdown-ish analysis to very basic HTML paragraphs
    analysis_html = _markdown_to_html(analysis)
    return f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: sans-serif; max-width: 900px; margin: 2em auto; }}
  h1 {{ color: #c0392b; }}
  h2 {{ color: #555; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
  pre {{ background: #f4f4f4; padding: 1em; overflow-x: auto; white-space: pre-wrap;
         font-size: 0.85em; }}
  .meta {{ color: #666; margin-bottom: 1.5em; }}
  .analysis {{ line-height: 1.6; }}
</style>
</head>
<body>
<h1>⚠️ SELinux AVC Denial Alert</h1>
<p class="meta">
  <b>Host:</b> {escape_html(hostname)}<br>
  <b>Time:</b> {escape_html(now)}<br>
  <b>Denial count:</b> {count}
</p>
<h2>Raw Denials</h2>
<pre>{escape_html(raw_block)}</pre>
<h2>LLM Analysis</h2>
<div class="analysis">{analysis_html}</div>
</body>
</html>"""


def _markdown_to_html(text):
    """Backward-compatible alias for :func:`server_watchdog.utils.markdown_to_html`."""
    return markdown_to_html(text)
