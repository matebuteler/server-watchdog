import subprocess
from unittest.mock import MagicMock, patch, call

import pytest

from server_watchdog.config import Config
from server_watchdog.email_sender import send_email, _send_email_msmtp, _build_message, _resolve_msmtp_env


def _make_config(**overrides):
    cfg = Config(config_path="/tmp/does_not_exist_watchdog.ini")
    for section_key, value in overrides.items():
        section, key = section_key.split(".", 1)
        cfg._parser.set(section, key, value)
    return cfg


class TestBuildMessage:
    def test_plain_message(self):
        cfg = _make_config()
        msg, subj, from_a, to_a = _build_message(cfg, "Test", "body text")
        assert "[server-watchdog] Test" in subj
        assert from_a == "watchdog@localhost"
        assert to_a == "root@localhost"
        assert "body text" in msg.as_string()

    def test_html_message(self):
        cfg = _make_config()
        msg, _, _, _ = _build_message(cfg, "Test", "plain", body_html="<b>html</b>")
        msg_str = msg.as_string()
        assert "plain" in msg_str
        assert "html" in msg_str


class TestMsmtpBackend:
    def test_msmtp_dispatched(self):
        """backend=msmtp routes through _send_email_msmtp."""
        cfg = _make_config(**{"email.backend": "msmtp"})

        with patch("server_watchdog.email_sender._send_email_msmtp") as mock_msmtp:
            send_email(cfg, "Test", "body")
        mock_msmtp.assert_called_once()

    def test_smtp_dispatched_by_default(self):
        """backend=smtp (default) routes through _send_email_smtp."""
        cfg = _make_config()

        with patch("server_watchdog.email_sender._send_email_smtp") as mock_smtp:
            send_email(cfg, "Test", "body")
        mock_smtp.assert_called_once()

    def test_msmtp_sends_via_subprocess(self):
        """msmtp backend pipes message through subprocess."""
        cfg = _make_config(**{"email.backend": "msmtp"})
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            _send_email_msmtp(cfg, "Test", "body text")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "msmtp"
        assert "--read-envelope-from" in cmd
        assert "--read-recipients" in cmd

    def test_msmtp_with_account(self):
        """msmtp -a <account> is passed when msmtp_account is set."""
        cfg = _make_config(**{
            "email.backend": "msmtp",
            "email.msmtp_account": "gmail",
        })
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            _send_email_msmtp(cfg, "Test", "body")

        cmd = mock_run.call_args[0][0]
        assert "-a" in cmd
        assert "gmail" in cmd

    def test_msmtp_custom_binary(self):
        """msmtp_bin config overrides the binary path."""
        cfg = _make_config(**{
            "email.backend": "msmtp",
            "email.msmtp_bin": "/usr/local/bin/msmtp",
        })
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            _send_email_msmtp(cfg, "Test", "body")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/msmtp"

    def test_msmtp_nonzero_exit_raises(self):
        """Non-zero msmtp exit code raises RuntimeError."""
        cfg = _make_config(**{"email.backend": "msmtp"})
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "authentication failed"

        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="authentication failed"):
                _send_email_msmtp(cfg, "Test", "body")

    def test_msmtp_not_found_raises(self):
        """Missing msmtp binary raises RuntimeError with install hint."""
        cfg = _make_config(**{"email.backend": "msmtp"})

        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="msmtp binary not found"):
                _send_email_msmtp(cfg, "Test", "body")

    def test_msmtp_timeout_raises(self):
        """msmtp timeout raises RuntimeError."""
        cfg = _make_config(**{"email.backend": "msmtp"})

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("msmtp", 120)):
            with pytest.raises(RuntimeError, match="timed out"):
                _send_email_msmtp(cfg, "Test", "body")

    def test_message_piped_as_input(self):
        """The full MIME message is passed as stdin to msmtp."""
        cfg = _make_config(**{"email.backend": "msmtp"})
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            _send_email_msmtp(cfg, "Test", "my body content")

        kwargs = mock_run.call_args[1]
        assert "my body content" in kwargs.get("input", "")

    def test_msmtp_config_file_flag(self):
        """msmtp_config_file passes --file to msmtp."""
        cfg = _make_config(**{
            "email.backend": "msmtp",
            "email.msmtp_config_file": "/home/mateo/.msmtprc",
        })
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            _send_email_msmtp(cfg, "Test", "body")

        cmd = mock_run.call_args[0][0]
        assert "--file" in cmd
        assert "/home/mateo/.msmtprc" in cmd


class TestResolveMsmtpEnv:
    def test_no_sudo_user_returns_current_env(self):
        """Without SUDO_USER the env is passed through unchanged (minus that key)."""
        env = {"HOME": "/root", "PATH": "/usr/bin"}
        with patch("os.environ", env):
            result = _resolve_msmtp_env()
        assert result["HOME"] == "/root"

    def test_sudo_user_restores_home(self):
        """SUDO_USER present → HOME is rewritten to that user's home dir."""
        import pwd as _pwd
        fake_pw = _pwd.struct_passwd(
            ("mateo", "x", 1000, 1000, "", "/home/mateo", "/bin/bash")
        )
        env = {"HOME": "/root", "SUDO_USER": "mateo", "PATH": "/usr/bin"}
        with patch("os.environ", env), \
             patch("pwd.getpwnam", return_value=fake_pw):
            result = _resolve_msmtp_env()
        assert result["HOME"] == "/home/mateo"
        assert result["USER"] == "mateo"

    def test_sudo_user_unknown_leaves_home_unchanged(self):
        """Unknown SUDO_USER (not in passwd) leaves HOME unchanged."""
        env = {"HOME": "/root", "SUDO_USER": "ghost", "PATH": "/usr/bin"}
        with patch("os.environ", env), \
             patch("pwd.getpwnam", side_effect=KeyError("ghost")):
            result = _resolve_msmtp_env()
        assert result["HOME"] == "/root"

