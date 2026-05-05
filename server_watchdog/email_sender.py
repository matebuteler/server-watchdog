"""Email delivery for server-watchdog reports and alerts."""

import logging
import os
import pwd
import smtplib
import ssl
import subprocess
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def send_email(config, subject, body_text, body_html=None):
    """Send an email using the configured backend.

    Parameters
    ----------
    config:
        A :class:`~server_watchdog.config.Config` instance.
    subject:
        Email subject (the configured prefix is prepended automatically).
    body_text:
        Plain-text body.
    body_html:
        Optional HTML body.  When provided a multipart/alternative message
        is sent; otherwise a plain text message is used.
    """
    backend = config.get("email", "backend", fallback="smtp").lower()
    if backend == "msmtp":
        _send_email_msmtp(config, subject, body_text, body_html)
    else:
        _send_email_smtp(config, subject, body_text, body_html)


def _build_message(config, subject, body_text, body_html=None):
    """Build a MIME message from the given parameters.

    Returns ``(msg, full_subject, from_addr, to_addr)``.
    """
    prefix = config.get("email", "subject_prefix", fallback="[server-watchdog]")
    full_subject = f"{prefix} {subject}"

    from_addr = config.get("email", "from_addr")
    to_addr = config.get("email", "to_addr")

    if body_html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))
    else:
        msg = MIMEText(body_text, "plain")

    msg["Subject"] = full_subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    return msg, full_subject, from_addr, to_addr


def _send_email_smtp(config, subject, body_text, body_html=None):
    """Send an email using Python's smtplib (the original backend)."""
    msg, full_subject, from_addr, to_addr = _build_message(
        config, subject, body_text, body_html
    )

    smtp_host = config.get("email", "smtp_host")
    smtp_port = config.getint("email", "smtp_port")
    use_tls = config.getboolean("email", "use_tls")
    use_starttls = config.getboolean("email", "use_starttls")
    username = config.get("email", "username")
    password = config.get("email", "password")

    try:
        if use_tls:
            smtp = smtplib.SMTP_SSL(smtp_host, smtp_port)
        else:
            smtp = smtplib.SMTP(smtp_host, smtp_port)
            if use_starttls:
                smtp.starttls()

        if username:
            smtp.login(username, password)

        smtp.sendmail(from_addr, [to_addr], msg.as_string())
        smtp.quit()
        logger.info("Email sent: %s", full_subject)
    except ssl.SSLError as exc:
        hint = (
            f"SSL handshake with {smtp_host}:{smtp_port} failed ({exc}). "
            "Check your [email] settings in config.ini:\n"
            "  - If your server uses plain SMTP (port 25): "
            "set use_tls = false and use_starttls = false\n"
            "  - If your server uses STARTTLS (port 587): "
            "set use_tls = false and use_starttls = true\n"
            "  - If your server uses implicit TLS (port 465): "
            "set use_tls = true and use_starttls = false"
        )
        logger.error(hint)
        raise RuntimeError(hint) from exc
    except Exception as exc:
        logger.error("Failed to send email '%s': %s", full_subject, exc)
        raise


def _resolve_msmtp_env():
    """Return an environment dict for the msmtp subprocess.

    When server-watchdog runs under ``sudo``, the process ``HOME`` is set to
    ``/root``, which causes msmtp to report "no configuration file available"
    because it cannot find the invoking user's ``~/.msmtprc``.

    This function detects ``SUDO_USER`` and rewrites ``HOME`` to that user's
    actual home directory so msmtp locates its config file transparently.

    Returns
    -------
    dict
        A copy of the current environment with ``HOME`` (and ``USER``) corrected
        to the sudo-invoking user when applicable.
    """
    env = os.environ.copy()
    sudo_user = env.get("SUDO_USER")
    if sudo_user:
        try:
            pw = pwd.getpwnam(sudo_user)
            env["HOME"] = pw.pw_dir
            env["USER"] = sudo_user
            logger.debug(
                "msmtp: running under sudo as %s; setting HOME=%s",
                sudo_user, pw.pw_dir,
            )
        except KeyError:
            logger.warning(
                "msmtp: SUDO_USER=%s not found in passwd; HOME unchanged.",
                sudo_user,
            )
    return env


def _send_email_msmtp(config, subject, body_text, body_html=None):
    """Send an email by piping through the ``msmtp`` command-line tool.

    This backend allows users to reuse their existing ``~/.msmtprc``
    configuration (e.g. Gmail with App Passwords) without duplicating
    SMTP settings in ``config.ini``.

    The ``msmtp`` binary is invoked with ``--read-envelope-from`` (reads
    the ``From:`` header) and ``--read-recipients`` (reads ``To:``/``Cc:``
    headers).  An optional account name can be specified via the
    ``msmtp_account`` config key (maps to ``msmtp -a <account>``).

    An optional ``msmtp_config_file`` key may be set to an explicit path
    to the msmtprc file (useful when the file is not in the default location).

    When invoked via ``sudo``, ``HOME`` is restored to the invoking user's
    home directory automatically via :func:`_resolve_msmtp_env`.
    """
    msg, full_subject, from_addr, to_addr = _build_message(
        config, subject, body_text, body_html
    )

    msmtp_bin = config.get("email", "msmtp_bin", fallback="msmtp")
    msmtp_account = config.get("email", "msmtp_account", fallback="")
    msmtp_config_file = config.get("email", "msmtp_config_file", fallback="")

    cmd = [msmtp_bin, "--read-envelope-from", "--read-recipients"]
    if msmtp_account:
        cmd.extend(["-a", msmtp_account])
    if msmtp_config_file:
        cmd.extend(["--file", msmtp_config_file])

    env = _resolve_msmtp_env()

    try:
        proc = subprocess.run(
            cmd,
            input=msg.as_string(),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if proc.returncode != 0:
            error_msg = proc.stderr.strip() or f"msmtp exited with code {proc.returncode}"
            logger.error("msmtp failed: %s", error_msg)
            raise RuntimeError(f"msmtp failed: {error_msg}")
        logger.info("Email sent via msmtp: %s", full_subject)
    except FileNotFoundError:
        hint = (
            f"msmtp binary not found at '{msmtp_bin}'. "
            "Install msmtp (e.g. 'dnf install msmtp' or 'zypper install msmtp') "
            "or set msmtp_bin in [email] to the correct path."
        )
        logger.error(hint)
        raise RuntimeError(hint) from None
    except subprocess.TimeoutExpired:
        logger.error("msmtp timed out after 120 seconds.")
        raise RuntimeError("msmtp timed out after 120 seconds.") from None
