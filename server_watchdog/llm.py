"""LLM integration for analysing SELinux AVC denials via Google Gemini."""

import logging

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT_TEMPLATE = """\
You are a Linux security expert specialising in SELinux policy analysis.

Analyse the following raw SELinux AVC denial log entries collected on a RHEL 8 server.

For each **distinct** denial (group near-duplicate messages), provide:

1. **Summary:** A brief, human-readable explanation of what process tried to do \
what to which target.
2. **Severity:** One of Low / Medium / High / Critical, based on the context \
(e.g. an unprivileged user process touching /etc/shadow is Critical).
3. **Recommended Action:** Step-by-step advice on how to investigate and remediate \
the issue.  Do NOT recommend `audit2allow` unless it is strictly necessary for a \
custom local application with no existing policy module.

--- RAW AVC DENIALS ---
{raw_denials}
--- END ---

Respond in clear, well-structured Markdown.
"""

MAINTENANCE_REPORT_PROMPT_TEMPLATE = """\
You are a senior Linux systems administrator reviewing an automated maintenance \
report for the following server.

Server context: {server_context}
Hostname: {hostname}
Report date/time: {timestamp}

UID-to-username mapping (use usernames instead of UIDs in your report):
{uid_map_text}

Review the raw system data below and produce a concise, GLANCEABLE maintenance \
report in Markdown. Follow these rules strictly:

1. Start with a single line: overall health indicator + short headline.
   Use one of: 🔴 Critical | ⚠️ Needs Attention | ✅ Healthy
   Then 2-3 sentences of executive summary.

2. Order sections by urgency — critical/action-required sections FIRST, \
all-clear sections LAST.  Omit sections that have nothing worth reporting.

3. PACKAGE UPDATES: When updates are available, classify each package:
   - 🔴 Security/Critical: kernel, openssl, glibc, nss, sudo, polkit, or packages \
tied to known CVEs
   - ⚠️ Important: runtime libraries, daemons, compiler toolchains
   - ℹ️ Routine: documentation, minor utilities, fonts
   Group packages by priority tier.  If all packages are up to date, omit this section.

4. FAILED SERVICES: For each failed service, examine the provided log snippet.
   - Skip services that are irrelevant to this server's context (e.g. \
bluetooth.service, pulseaudio.service, avahi-daemon.service are noise on an EDA \
workstation with no audio or Bluetooth).
   - For genuinely relevant failures, provide a 1-2 sentence assessment \
and actionable recommendation.
   If all failures are irrelevant noise for this server, say so in one line and \
omit further detail.

5. STORAGE: Report only filesystems above the usage threshold.
   NFS mounts must appear LAST within this section, labelled as \
"lower priority — remote storage".

6. COREDUMPS: For each coredump, identify the crashed binary, the signal, \
and whether it looks like a known issue. Assess severity briefly.
   If there are no recent coredumps, omit this section entirely.

7. JOURNAL ERRORS: Summarise the most important distinct error patterns \
(collapse repetitive lines into counts).  Skip boot-time noise, \
NetworkManager link-watch messages, and other expected transient messages.

8. Keep the total report short enough to read in under 60 seconds.
   Use emoji indicators consistently: 🔴 critical, ⚠️ warning, \
✅ ok/no action needed, ℹ️ informational.

--- RAW DATA ---
{raw_data_text}
--- END RAW DATA ---

Now write the glanceable report:
"""


def analyse_avc_denials(config, raw_denials):
    """Send *raw_denials* to the configured LLM and return the analysis text.

    Parameters
    ----------
    config:
        A :class:`~server_watchdog.config.Config` instance.
    raw_denials:
        A list of raw AVC denial log strings.

    Returns
    -------
    str
        Markdown-formatted analysis from the LLM, or an error message if the
        call fails.
    """
    provider = config.get("llm", "provider", fallback="gemini").lower()
    api_key = config.get("llm", "api_key", fallback="")
    model_name = config.get("llm", "model", fallback="gemini-1.5-pro")

    if not api_key:
        logger.warning("LLM API key is not configured; skipping analysis.")
        return "(LLM analysis unavailable: no API key configured.)"

    prompt = ANALYSIS_PROMPT_TEMPLATE.format(raw_denials="\n".join(raw_denials))

    if provider == "gemini":
        return _call_gemini(api_key, model_name, prompt)

    logger.error("Unknown LLM provider: %s", provider)
    return f"(LLM analysis unavailable: unknown provider '{provider}'.)"


def analyse_maintenance_report(config, raw):
    """Send raw maintenance data to the LLM and return a glanceable Markdown report.

    Parameters
    ----------
    config:
        A :class:`~server_watchdog.config.Config` instance.
    raw:
        A dict produced by :func:`server_watchdog.maintenance.build_report`
        containing collected system data (packages, services, storage, etc.).

    Returns
    -------
    str
        Markdown-formatted report from the LLM, or a sentinel string starting
        with ``"(LLM"`` if the call fails or is unconfigured.
    """
    provider = config.get("llm", "provider", fallback="gemini").lower()
    api_key = config.get("llm", "api_key", fallback="")
    model_name = config.get("llm", "model", fallback="gemini-1.5-pro")

    if not api_key:
        logger.warning("LLM API key is not configured; skipping maintenance analysis.")
        return "(LLM analysis unavailable: no API key configured.)"

    prompt = _build_maintenance_prompt(raw)

    if provider == "gemini":
        return _call_gemini(api_key, model_name, prompt)

    logger.error("Unknown LLM provider: %s", provider)
    return f"(LLM analysis unavailable: unknown provider '{provider}'.)"


def _build_maintenance_prompt(raw):
    """Format the raw maintenance data dict into the LLM prompt string."""
    # ── UID map ───────────────────────────────────────────────────────────
    uid_map = raw.get("uid_map", {})
    if uid_map:
        uid_map_text = "\n".join(
            f"  UID {uid}: {name}" for uid, name in sorted(uid_map.items())
        )
    else:
        uid_map_text = "  (not available)"

    # ── Packages ──────────────────────────────────────────────────────────
    pkg = raw.get("packages")
    if pkg is None:
        packages_section = "Package check disabled."
    elif pkg.get("error"):
        packages_section = f"Error: {pkg['error']}"
    elif pkg.get("updates"):
        packages_section = (
            f"{len(pkg['updates'])} update(s) available:\n"
            + "\n".join(pkg["updates"])
        )
    else:
        packages_section = "All packages are up to date."

    # ── Failed services ───────────────────────────────────────────────────
    svc = raw.get("services")
    if svc is None:
        services_section = "Service check disabled."
    elif svc.get("error"):
        services_section = f"Error: {svc['error']}"
    elif svc.get("failed"):
        parts = []
        logs = svc.get("logs", {})
        for unit_line in svc["failed"]:
            words = unit_line.split()
            unit_name = words[0].lstrip("●✗× ") if words else unit_line
            # Prefer the key that matches the unit name (first non-symbol word)
            log_text = logs.get(unit_name, "")
            parts.append(f"Unit: {unit_line}")
            if log_text:
                parts.append(f"Recent logs:\n{log_text}")
            parts.append("")
        services_section = "\n".join(parts).strip()
    else:
        services_section = "No failed services."

    # ── Storage ───────────────────────────────────────────────────────────
    sto = raw.get("storage")
    threshold = raw.get("threshold", 80)
    if sto is None:
        storage_section = "Storage check disabled."
    elif sto.get("error"):
        storage_section = f"Error: {sto['error']}"
    else:
        local_fs = sto.get("filesystems", [])
        nfs_fs = sto.get("nfs_filesystems", [])
        all_out = sto.get("all_output", "")
        if local_fs or nfs_fs:
            lines = []
            if local_fs:
                lines.append(f"Local filesystems above {threshold}%:")
                lines.extend(local_fs)
            if nfs_fs:
                lines.append(f"\nNFS mounts above {threshold}% (lower priority):")
                lines.extend(nfs_fs)
            lines.append(f"\nFull disk usage:\n{all_out}")
            storage_section = "\n".join(lines)
        else:
            storage_section = (
                f"All filesystems below {threshold}% usage.\n\n"
                f"Full disk usage:\n{all_out}"
            )

    # ── Coredumps ─────────────────────────────────────────────────────────
    core = raw.get("coredumps", {})
    coredump_age = raw.get("coredump_age", 45)
    if core.get("error"):
        coredumps_section = f"Note: {core['error']}"
    elif core.get("dumps"):
        coredumps_section = "\n".join(core["dumps"])
    else:
        coredumps_section = f"No coredumps in the last {coredump_age} days."

    # ── Journal errors ────────────────────────────────────────────────────
    jnl = raw.get("journal_errors", {})
    lookback = raw.get("lookback", 30)
    if jnl.get("error"):
        journal_section = f"Error: {jnl['error']}"
    elif jnl.get("errors"):
        journal_section = (
            f"{len(jnl['errors'])} error/critical message(s) in the last {lookback} days:\n"
            + "\n".join(jnl["errors"])
        )
    else:
        journal_section = f"No error/critical messages in the last {lookback} days."

    raw_data_text = (
        f"### Package Updates\n{packages_section}\n\n"
        f"### Failed Services\n{services_section}\n\n"
        f"### Storage Usage (threshold: {threshold}%)\n{storage_section}\n\n"
        f"### Coredumps (last {coredump_age} days)\n{coredumps_section}\n\n"
        f"### Journal Errors (last {lookback} days)\n{journal_section}"
    )

    return MAINTENANCE_REPORT_PROMPT_TEMPLATE.format(
        server_context=raw.get("server_context", "Linux server"),
        hostname=raw.get("hostname", "unknown"),
        timestamp=raw.get("timestamp", "unknown"),
        uid_map_text=uid_map_text,
        raw_data_text=raw_data_text,
    )


def _call_gemini(api_key, model_name, prompt):
    """Call the Google Gemini API and return the response text."""
    try:
        from google import genai  # pylint: disable=import-outside-toplevel

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model_name, contents=prompt)
        return response.text
    except ImportError:
        logger.error(
            "google-genai package is not installed. "
            "Install it with: pip install google-genai"
        )
        return "(LLM analysis unavailable: google-genai not installed.)"
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Gemini API call failed: %s", exc)
        return f"(LLM analysis failed: {exc})"
