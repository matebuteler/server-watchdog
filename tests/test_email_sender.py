"""Tests for server_watchdog.email_sender."""

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from server_watchdog.config import Config
from server_watchdog.email_sender import send_email


def _make_config(**overrides):
    cfg = Config(config_path="/tmp/does_not_exist_watchdog.ini")
    for section_key, value in overrides.items():
        section, key = section_key.split(".", 1)
        cfg._parser.set(section, key, value)
    return cfg


class TestSendEmail:
    def test_plain_text_sent(self):
        cfg = _make_config()
        mock_smtp = MagicMock()

        with patch("smtplib.SMTP", return_value=mock_smtp) as smtp_cls:
            send_email(cfg, "Test subject", "Hello world")

        smtp_cls.assert_called_once_with("localhost", 25)
        assert mock_smtp.sendmail.called
        call_args = mock_smtp.sendmail.call_args
        from_arg, to_arg, msg_str = call_args[0]
        assert from_arg == "watchdog@localhost"
        assert "root@localhost" in to_arg
        assert "Hello world" in msg_str
        assert "[server-watchdog] Test subject" in msg_str

    def test_subject_prefix_included(self):
        cfg = _make_config()
        mock_smtp = MagicMock()

        with patch("smtplib.SMTP", return_value=mock_smtp):
            send_email(cfg, "Monthly Report", "body")

        _, _, msg_str = mock_smtp.sendmail.call_args[0]
        assert "[server-watchdog] Monthly Report" in msg_str

    def test_html_message_sent(self):
        cfg = _make_config()
        mock_smtp = MagicMock()

        with patch("smtplib.SMTP", return_value=mock_smtp):
            send_email(cfg, "Alert", "plain body", body_html="<b>html body</b>")

        _, _, msg_str = mock_smtp.sendmail.call_args[0]
        assert "plain body" in msg_str
        assert "html body" in msg_str

    def test_tls_uses_smtp_ssl(self):
        cfg = _make_config(**{"email.use_tls": "true"})
        mock_smtp = MagicMock()

        with patch("smtplib.SMTP_SSL", return_value=mock_smtp) as ssl_cls:
            send_email(cfg, "s", "b")

        assert ssl_cls.called

    def test_smtp_error_raises(self):
        cfg = _make_config()

        with patch("smtplib.SMTP", side_effect=smtplib.SMTPException("connection refused")):
            with pytest.raises(smtplib.SMTPException):
                send_email(cfg, "s", "b")

    def test_login_called_when_credentials_set(self):
        cfg = _make_config(**{"email.username": "user", "email.password": "pass"})
        mock_smtp = MagicMock()

        with patch("smtplib.SMTP", return_value=mock_smtp):
            send_email(cfg, "s", "b")

        mock_smtp.login.assert_called_once_with("user", "pass")

    def test_starttls_called_when_use_starttls_true(self):
        cfg = _make_config(**{"email.use_starttls": "true"})
        mock_smtp = MagicMock()

        with patch("smtplib.SMTP", return_value=mock_smtp):
            send_email(cfg, "s", "b")

        mock_smtp.starttls.assert_called_once()

    def test_starttls_not_called_by_default(self):
        cfg = _make_config()
        mock_smtp = MagicMock()

        with patch("smtplib.SMTP", return_value=mock_smtp):
            send_email(cfg, "s", "b")

        mock_smtp.starttls.assert_not_called()

    def test_starttls_not_called_when_use_tls_true(self):
        """SMTP_SSL path: starttls() must not be called (it's already encrypted)."""
        cfg = _make_config(**{"email.use_tls": "true"})
        mock_smtp = MagicMock()

        with patch("smtplib.SMTP_SSL", return_value=mock_smtp):
            send_email(cfg, "s", "b")

        mock_smtp.starttls.assert_not_called()

    def test_ssl_error_raises_runtime_error_with_hint(self):
        """An ssl.SSLError should be re-raised as RuntimeError with config hints."""
        import ssl  # pylint: disable=import-outside-toplevel
        cfg = _make_config(**{"email.use_tls": "true"})

        with patch("smtplib.SMTP_SSL", side_effect=ssl.SSLError("WRONG_VERSION_NUMBER")):
            with pytest.raises(RuntimeError) as exc_info:
                send_email(cfg, "s", "b")

        msg = str(exc_info.value)
        assert "use_tls" in msg
        assert "use_starttls" in msg

    def test_ssl_error_on_plain_smtp_raises_runtime_error_with_hint(self):
        """ssl.SSLError on a plain SMTP connection is also wrapped with a hint."""
        import ssl  # pylint: disable=import-outside-toplevel
        cfg = _make_config()  # use_tls = false, use_starttls = false

        mock_smtp = MagicMock()
        mock_smtp.starttls.side_effect = ssl.SSLError("WRONG_VERSION_NUMBER")

        # Trigger via starttls path
        cfg2 = _make_config(**{"email.use_starttls": "true"})
        mock_smtp2 = MagicMock()
        mock_smtp2.starttls.side_effect = ssl.SSLError("WRONG_VERSION_NUMBER")

        with patch("smtplib.SMTP", return_value=mock_smtp2):
            with pytest.raises(RuntimeError) as exc_info:
                send_email(cfg2, "s", "b")

        assert "use_tls" in str(exc_info.value)
