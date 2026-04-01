"""Shared utility helpers for server-watchdog."""

import re
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


def _apply_inline_markdown(text):
    """Apply inline Markdown rules to already-HTML-escaped text.

    Handles inline code (`` `…` ``) and bold (``**…**``).  Code spans are
    extracted first and replaced with placeholders so that asterisks inside
    backtick spans are never interpreted as bold markers.
    """
    # Extract code spans, replace with placeholders, then restore after bold.
    code_spans = []

    def _extract_code(m):
        placeholder = f"\x00CODE{len(code_spans)}\x00"
        code_spans.append(f"<code>{m.group(1)}</code>")
        return placeholder

    text = re.sub(r"`(.+?)`", _extract_code, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    for i, span in enumerate(code_spans):
        text = text.replace(f"\x00CODE{i}\x00", span)
    return text


def markdown_to_html(text):
    """Very minimal Markdown-to-HTML conversion for LLM output.

    Handles headings (# / ## / ###), bold (**text**), inline code (`text`),
    bullet lists (``- `` / ``* ``), horizontal rules (---), and blank lines.
    """
    html_lines = []
    for line in text.splitlines():
        # Headings
        if line.startswith("### "):
            html_lines.append(f"<h4>{escape_html(line[4:])}</h4>")
        elif line.startswith("## "):
            html_lines.append(f"<h3>{escape_html(line[3:])}</h3>")
        elif line.startswith("# "):
            html_lines.append(f"<h2>{escape_html(line[2:])}</h2>")
        # Horizontal rule
        elif re.match(r"^-{3,}$", line):
            html_lines.append("<hr>")
        # Bullet points
        elif line.startswith("- ") or line.startswith("* "):
            html_lines.append(
                f"<li>{_apply_inline_markdown(escape_html(line[2:]))}</li>"
            )
        # Blank line → spacer
        elif not line.strip():
            html_lines.append("<br>")
        else:
            escaped = _apply_inline_markdown(escape_html(line))
            html_lines.append(f"<p>{escaped}</p>")
    return "\n".join(html_lines)


def get_uid_map():
    """Return a dict mapping UID (int) to username by reading ``/etc/passwd``.

    Returns an empty dict if the file is unreadable.
    """
    uid_map = {}
    try:
        with open("/etc/passwd", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(":")
                if len(parts) >= 3:
                    try:
                        uid_map[int(parts[2])] = parts[0]
                    except ValueError:
                        continue
    except OSError:
        pass
    return uid_map
