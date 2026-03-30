"""Tests for server_watchdog.config."""

import configparser
import os
import tempfile

import pytest

from server_watchdog.config import Config


class TestConfigDefaults:
    """Config returns sensible defaults when no file exists."""

    def setup_method(self):
        # Point at a path that definitely doesn't exist
        self.cfg = Config(config_path="/tmp/does_not_exist_watchdog.ini")

    def test_email_smtp_host(self):
        assert self.cfg.get("email", "smtp_host") == "localhost"

    def test_email_smtp_port(self):
        assert self.cfg.getint("email", "smtp_port") == 25

    def test_email_use_tls_false(self):
        assert self.cfg.getboolean("email", "use_tls") is False

    def test_llm_provider(self):
        assert self.cfg.get("llm", "provider") == "gemini"

    def test_llm_model(self):
        assert self.cfg.get("llm", "model") == "gemini-1.5-pro"

    def test_maintenance_threshold(self):
        assert self.cfg.getint("maintenance", "storage_threshold") == 80

    def test_avc_batch_interval(self):
        assert self.cfg.getint("avc_monitor", "batch_interval") == 60

    def test_logging_level(self):
        assert self.cfg.get("logging", "level") == "INFO"


class TestConfigFileOverride:
    """Values in a config file override the defaults."""

    def test_override_smtp_host(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as fh:
            fh.write("[email]\nsmtp_host = mail.example.com\n")
            path = fh.name

        try:
            cfg = Config(config_path=path)
            assert cfg.get("email", "smtp_host") == "mail.example.com"
        finally:
            os.unlink(path)

    def test_override_api_key(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as fh:
            fh.write("[llm]\napi_key = supersecret\n")
            path = fh.name

        try:
            cfg = Config(config_path=path)
            assert cfg.get("llm", "api_key") == "supersecret"
        finally:
            os.unlink(path)

    def test_override_storage_threshold(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as fh:
            fh.write("[maintenance]\nstorage_threshold = 90\n")
            path = fh.name

        try:
            cfg = Config(config_path=path)
            assert cfg.getint("maintenance", "storage_threshold") == 90
        finally:
            os.unlink(path)

    def test_env_var_path(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "watchdog.ini"
        cfg_file.write_text("[email]\nto_addr = ops@example.com\n")
        monkeypatch.setenv("WATCHDOG_CONFIG", str(cfg_file))
        cfg = Config()
        assert cfg.get("email", "to_addr") == "ops@example.com"
