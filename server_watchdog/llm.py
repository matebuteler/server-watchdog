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


def _call_gemini(api_key, model_name, prompt):
    """Call the Google Gemini API and return the response text."""
    try:
        import google.generativeai as genai  # pylint: disable=import-outside-toplevel

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(prompt)
        return response.text
    except ImportError:
        logger.error(
            "google-generativeai package is not installed. "
            "Install it with: pip install google-generativeai"
        )
        return "(LLM analysis unavailable: google-generativeai not installed.)"
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Gemini API call failed: %s", exc)
        return f"(LLM analysis failed: {exc})"
