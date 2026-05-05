"""LLM integration for analysing SELinux/AppArmor denials via Google Gemini.

Supports cascading model fallback with per-model rate limiting.
"""

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

APPARMOR_ANALYSIS_PROMPT_TEMPLATE = """\
You are a Linux security expert specialising in AppArmor profile analysis.

Analyse the following raw AppArmor denial log entries collected on an openSUSE server.

For each **distinct** denial (group near-duplicate messages), provide:

1. **Summary:** A brief, human-readable explanation of what process tried to do \
what to which target, including the AppArmor profile involved.
2. **Severity:** One of Low / Medium / High / Critical, based on the context \
(e.g. an unprivileged process accessing /etc/shadow is Critical).
3. **Recommended Action:** Step-by-step advice on how to investigate and remediate \
the issue.  Reference AppArmor tools where appropriate (aa-logprof, aa-enforce, \
aa-complain, profile editing in /etc/apparmor.d/).  Do NOT recommend disabling \
AppArmor unless there is no reasonable alternative.

--- RAW APPARMOR DENIALS ---
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


def analyse_avc_denials(config, raw_denials, mac_system="selinux"):
    """Send *raw_denials* to the configured LLM and return the analysis text.

    Parameters
    ----------
    config:
        A :class:`~server_watchdog.config.Config` instance.
    raw_denials:
        A list of raw AVC/AppArmor denial log strings.
    mac_system:
        ``"selinux"`` or ``"apparmor"`` — selects the appropriate prompt.

    Returns
    -------
    str
        Markdown-formatted analysis from the LLM, or an error message if the
        call fails.
    """
    provider = config.get("llm", "provider", fallback="gemini").lower()
    api_key = config.get("llm", "api_key", fallback="")
    model_name = config.get("llm", "model", fallback="gemini-3-flash-preview")

    if not api_key:
        logger.warning("LLM API key is not configured; skipping analysis.")
        return "(LLM analysis unavailable: no API key configured.)"

    if mac_system == "apparmor":
        prompt = APPARMOR_ANALYSIS_PROMPT_TEMPLATE.format(
            raw_denials="\n".join(raw_denials)
        )
    else:
        prompt = ANALYSIS_PROMPT_TEMPLATE.format(
            raw_denials="\n".join(raw_denials)
        )

    if provider == "gemini":
        return _call_gemini(config, api_key, model_name, prompt)

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
    model_name = config.get("llm", "model", fallback="gemini-3-flash-preview")

    if not api_key:
        logger.warning("LLM API key is not configured; skipping maintenance analysis.")
        return "(LLM analysis unavailable: no API key configured.)"

    prompt = _build_maintenance_prompt(raw)

    if provider == "gemini":
        return _call_gemini(config, api_key, model_name, prompt)

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


def _call_gemini(config, api_key, model_name, prompt):
    """Call the Google Gemini API with rate limiting and cascading fallback.

    Parameters
    ----------
    config:
        A :class:`~server_watchdog.config.Config` instance (used to read
        rate-limiting settings).
    api_key:
        The Gemini API key.
    model_name:
        The requested model codename.
    prompt:
        The prompt text to send.
    """
    try:
        from google import genai  # pylint: disable=import-outside-toplevel
        from google.genai import types as genai_types  # pylint: disable=import-outside-toplevel
        from .rate_limiter import RateLimiter, estimate_tokens  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        if "genai" in str(exc):
            logger.error(
                "google-genai package is not installed. "
                "Install it with: pip install google-genai"
            )
            return "(LLM analysis unavailable: google-genai not installed.)"
        raise

    # ── Set up rate limiter ────────────────────────────────────────────────
    no_fallback = config.getboolean("llm", "no_fallback", fallback=False)
    state_path = config.get("llm", "rate_limit_state", fallback="") or None
    chain_str = config.get(
        "llm", "fallback_chain",
        fallback="gemma-4-31b-it,gemini-3.1-flash-lite-preview",
    )
    fallback_chain = [model_name] + [
        m.strip() for m in chain_str.split(",") if m.strip()
    ]
    # Deduplicate while preserving order
    seen = set()
    unique_chain = []
    for m in fallback_chain:
        if m not in seen:
            seen.add(m)
            unique_chain.append(m)
    fallback_chain = unique_chain

    limiter = RateLimiter(
        state_path=state_path,
        no_fallback=no_fallback,
        fallback_chain=fallback_chain,
    )

    # ── Estimate tokens and pick the model ─────────────────────────────────
    est_prompt_tokens = estimate_tokens(prompt)
    # Assume response will be roughly 1/3 of prompt size
    est_response_tokens = max(200, est_prompt_tokens // 3)
    est_total = est_prompt_tokens + est_response_tokens

    actual_model = limiter.check_and_wait(est_total, model_name)
    if actual_model != model_name:
        logger.warning(
            "⚠️  Using fallback model %s instead of %s (rate limit reached). "
            "Response quality may be reduced.",
            actual_model, model_name,
        )

    # ── Search grounding pipeline (optional) ─────────────────────────────────
    # 3-step pipeline for maximum quality:
    #   1. Primary model → initial ungrounded analysis
    #   2. Search model (G2.5F) + grounding → enrich with real-time data
    #   3. Primary model → final grounded analysis
    # Uses 2x primary + 1x search calls.  Disable with search_grounding = false.
    search_grounding = config.getboolean("llm", "search_grounding", fallback=True)
    search_model = config.get(
        "llm", "search_grounding_model", fallback="gemini-2.5-flash"
    )

    if search_grounding:
        return _grounded_pipeline(
            api_key, actual_model, search_model, prompt,
            genai, genai_types, limiter, est_prompt_tokens,
        )

    # ── Simple (ungrounded) call ───────────────────────────────────────────
    return _simple_call(
        api_key, actual_model, prompt, genai, limiter, est_prompt_tokens,
    )


def _simple_call(api_key, model, prompt, genai, limiter, est_prompt_tokens):
    """Single-shot LLM call without search grounding."""
    from .rate_limiter import estimate_tokens  # pylint: disable=import-outside-toplevel

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        response_text = response.text

        resp_tokens = estimate_tokens(response_text) if response_text else 0
        limiter.record_usage(model, est_prompt_tokens, resp_tokens)

        return response_text
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Gemini API call failed (model=%s): %s", model, exc)
        return f"(LLM analysis failed: {exc})"


# ---------------------------------------------------------------------------
# 3-step search grounding pipeline
# ---------------------------------------------------------------------------

_GROUNDING_PROMPT = """\
You are a research assistant.  The following is a system administration analysis \
produced by an AI.  Search the internet for relevant and recent information that \
can validate, correct, or enrich this analysis.  Focus on:

- Known CVEs or security advisories for the packages, services, or policy \
modules mentioned
- Known bugs or regressions in the specific software versions
- Official best-practice recommendations or workarounds

Return ONLY the relevant facts you found, as concise bullet points with source \
URLs where available.  Do NOT rewrite the analysis — just provide supporting data.

--- INITIAL ANALYSIS ---
{initial_analysis}
--- END ---
"""

_REFINEMENT_PROMPT = """\
You previously produced the following analysis.  We have now retrieved real-time \
data from the internet to validate and enrich it.  Please produce your FINAL \
analysis by incorporating the grounded context below.  Reference specific CVEs, \
advisories, or known issues where the data supports it.  If the grounded context \
contradicts your initial analysis, correct it.

--- YOUR INITIAL ANALYSIS ---
{initial_analysis}
--- END INITIAL ANALYSIS ---

--- REAL-TIME GROUNDED CONTEXT ---
{grounded_context}
--- END GROUNDED CONTEXT ---

--- ORIGINAL REQUEST ---
{original_prompt}
--- END ORIGINAL REQUEST ---

Now produce the refined, grounded analysis:
"""


def _grounded_pipeline(api_key, primary_model, search_model, prompt,
                       genai, genai_types, limiter, est_prompt_tokens):
    """3-step grounded analysis pipeline.

    1. Primary model → initial analysis (ungrounded).
    2. Search model + Google Search tool → real-time context.
    3. Primary model → final analysis incorporating grounded context.

    Falls back to a simple ungrounded call if any grounding step fails.
    """
    from .rate_limiter import estimate_tokens  # pylint: disable=import-outside-toplevel

    client = genai.Client(api_key=api_key)

    # ── Step 1: Initial analysis ───────────────────────────────────────────
    logger.info("Grounding pipeline step 1/3: initial analysis via %s…", primary_model)
    try:
        resp1 = client.models.generate_content(model=primary_model, contents=prompt)
        initial_analysis = resp1.text or ""
        resp1_tokens = estimate_tokens(initial_analysis)
        limiter.record_usage(primary_model, est_prompt_tokens, resp1_tokens)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Grounding step 1 failed (model=%s): %s", primary_model, exc)
        return f"(LLM analysis failed: {exc})"

    if not initial_analysis.strip():
        return initial_analysis

    # ── Step 2: Search grounding ───────────────────────────────────────────
    logger.info("Grounding pipeline step 2/3: search grounding via %s…", search_model)
    grounding_prompt = _GROUNDING_PROMPT.format(
        initial_analysis=initial_analysis[:3000]
    )
    grounded_context = ""
    try:
        est_search_tokens = estimate_tokens(grounding_prompt) + 500
        actual_search_model = limiter.check_and_wait(est_search_tokens, search_model)

        grounding_tool = genai_types.Tool(
            google_search=genai_types.GoogleSearch()
        )
        search_config = genai_types.GenerateContentConfig(
            tools=[grounding_tool]
        )
        resp2 = client.models.generate_content(
            model=actual_search_model,
            contents=grounding_prompt,
            config=search_config,
        )
        grounded_context = resp2.text or ""
        resp2_tokens = estimate_tokens(grounded_context)
        limiter.record_usage(
            actual_search_model, estimate_tokens(grounding_prompt), resp2_tokens
        )
        logger.info(
            "Search grounding: fetched %d chars of context.", len(grounded_context)
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "Grounding step 2 failed (non-fatal, returning ungrounded result): %s",
            exc,
        )
        return initial_analysis  # graceful degradation

    if not grounded_context.strip():
        logger.info("Search grounding returned no context; using ungrounded result.")
        return initial_analysis

    # ── Step 3: Refined analysis ───────────────────────────────────────────
    logger.info("Grounding pipeline step 3/3: refined analysis via %s…", primary_model)
    refinement_prompt = _REFINEMENT_PROMPT.format(
        initial_analysis=initial_analysis,
        grounded_context=grounded_context,
        original_prompt=prompt[:2000],
    )
    try:
        est_refine_tokens = estimate_tokens(refinement_prompt)
        actual_refine_model = limiter.check_and_wait(
            est_refine_tokens + 500, primary_model
        )
        if actual_refine_model != primary_model:
            logger.warning(
                "⚠️  Refinement step using fallback model %s (rate limit on %s).",
                actual_refine_model, primary_model,
            )

        resp3 = client.models.generate_content(
            model=actual_refine_model, contents=refinement_prompt
        )
        final_text = resp3.text or initial_analysis
        resp3_tokens = estimate_tokens(final_text)
        limiter.record_usage(actual_refine_model, est_refine_tokens, resp3_tokens)

        return final_text
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "Grounding step 3 failed (returning ungrounded result): %s", exc
        )
        return initial_analysis
