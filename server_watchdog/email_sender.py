"""Email delivery for server-watchdog reports and alerts."""

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def send_email(config, subject, body_text, body_html=None):
    """Send an email using the SMTP settings from *config*.

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
