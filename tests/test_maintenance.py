"""Tests for server_watchdog.maintenance."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from server_watchdog.config import Config
from server_watchdog.maintenance import (
    check_failed_services,
    check_journal_errors,
    check_packages,
    check_storage,
    build_report,
)


def _make_config(**overrides):
    cfg = Config(config_path="/tmp/does_not_exist_watchdog.ini")
    for section_key, value in overrides.items():
        section, key = section_key.split(".", 1)
        cfg._parser.set(section, key, value)
    return cfg


def _completed(returncode, stdout="", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# check_packages
# ---------------------------------------------------------------------------

class TestCheckPackages:
    def test_no_updates(self):
        with patch("subprocess.run", return_value=_completed(0, "")):
            data = check_packages()
        assert data["error"] is None
        assert data["updates"] == []

    def test_updates_available(self):
        dnf_out = (
            "bash.x86_64                  5.1.8-6.el8  baseos\n"
            "curl.x86_64                  7.61.1-30.el8 baseos\n"
        )
        with patch("subprocess.run", return_value=_completed(100, dnf_out)):
            data = check_packages()
        assert data["error"] is None
        assert len(data["updates"]) == 2

    def test_dnf_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            data = check_packages()
        assert data["error"] is not None
        assert "dnf not found" in data["error"]

    def test_dnf_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("dnf", 300)):
            data = check_packages()
        assert "timed out" in data["error"]

    def test_dnf_error_exit(self):
        with patch("subprocess.run", return_value=_completed(1, "", "permission denied")):
            data = check_packages()
        assert data["error"] is not None


# ---------------------------------------------------------------------------
# check_failed_services
# ---------------------------------------------------------------------------

class TestCheckFailedServices:
    def test_no_failures(self):
        with patch("subprocess.run", return_value=_completed(0, "")):
            data = check_failed_services()
        assert data["failed"] == []
        assert data["error"] is None

    def test_failed_units(self):
        output = (
            "● myapp.service    loaded failed failed My Application\n"
            "● other.service    loaded failed failed Other\n"
        )
        with patch("subprocess.run", return_value=_completed(0, output)):
            data = check_failed_services()
        assert len(data["failed"]) == 2

    def test_systemctl_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            data = check_failed_services()
        assert "systemctl not found" in data["error"]


# ---------------------------------------------------------------------------
# check_storage
# ---------------------------------------------------------------------------

class TestCheckStorage:
    _DF_OUTPUT = (
        "Filesystem     Type   Size  Used Avail Use% Mounted on\n"
        "/dev/sda1      xfs    20G   16G  4G    82%  /\n"
        "/dev/sda2      xfs    10G    1G  9G    10%  /boot\n"
    )

    def test_above_threshold(self):
        with patch("subprocess.run", return_value=_completed(0, self._DF_OUTPUT)):
            data = check_storage(threshold=80)
        assert len(data["filesystems"]) == 1
        assert "/dev/sda1" in data["filesystems"][0]

    def test_none_above_threshold(self):
        with patch("subprocess.run", return_value=_completed(0, self._DF_OUTPUT)):
            data = check_storage(threshold=90)
        assert data["filesystems"] == []

    def test_df_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            data = check_storage()
        assert "df not found" in data["error"]


# ---------------------------------------------------------------------------
# check_journal_errors
# ---------------------------------------------------------------------------

class TestCheckJournalErrors:
    def test_no_errors(self):
        with patch("subprocess.run", return_value=_completed(0, "")):
            data = check_journal_errors()
        assert data["errors"] == []
        assert data["error"] is None

    def test_errors_returned(self):
        output = (
            "2024-01-15T12:00:00+0000 myhost kernel[0]: ERROR: disk failure\n"
            "2024-01-15T12:01:00+0000 myhost httpd[123]: CRITICAL: out of memory\n"
        )
        with patch("subprocess.run", return_value=_completed(0, output)):
            data = check_journal_errors(lookback_days=30)
        assert len(data["errors"]) == 2

    def test_journalctl_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            data = check_journal_errors()
        assert "journalctl not found" in data["error"]


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------

class TestBuildReport:
    def test_report_contains_hostname(self):
        cfg = _make_config()
        with (
            patch("server_watchdog.maintenance.check_packages",
                  return_value={"updates": [], "error": None}),
            patch("server_watchdog.maintenance.check_failed_services",
                  return_value={"failed": [], "error": None}),
            patch("server_watchdog.maintenance.check_storage",
                  return_value={"filesystems": [], "all_output": "", "threshold": 80, "error": None}),
            patch("server_watchdog.maintenance.check_journal_errors",
                  return_value={"errors": [], "error": None}),
            patch("server_watchdog.maintenance.get_hostname", return_value="testhost"),
        ):
            plain, html = build_report(cfg)

        assert "testhost" in plain
        assert "testhost" in html

    def test_report_has_html_structure(self):
        cfg = _make_config()
        with (
            patch("server_watchdog.maintenance.check_packages",
                  return_value={"updates": [], "error": None}),
            patch("server_watchdog.maintenance.check_failed_services",
                  return_value={"failed": [], "error": None}),
            patch("server_watchdog.maintenance.check_storage",
                  return_value={"filesystems": [], "all_output": "", "threshold": 80, "error": None}),
            patch("server_watchdog.maintenance.check_journal_errors",
                  return_value={"errors": [], "error": None}),
            patch("server_watchdog.maintenance.get_hostname", return_value="host"),
        ):
            _, html = build_report(cfg)

        assert "<!DOCTYPE html>" in html
        assert "<h1>" in html

    def test_packages_disabled(self):
        cfg = _make_config(**{"maintenance.check_packages": "false"})
        with (
            patch("server_watchdog.maintenance.check_packages") as mock_pkgs,
            patch("server_watchdog.maintenance.check_failed_services",
                  return_value={"failed": [], "error": None}),
            patch("server_watchdog.maintenance.check_storage",
                  return_value={"filesystems": [], "all_output": "", "threshold": 80, "error": None}),
            patch("server_watchdog.maintenance.check_journal_errors",
                  return_value={"errors": [], "error": None}),
            patch("server_watchdog.maintenance.get_hostname", return_value="host"),
        ):
            build_report(cfg)

        mock_pkgs.assert_not_called()

    def test_report_shows_failed_services(self):
        cfg = _make_config()
        with (
            patch("server_watchdog.maintenance.check_packages",
                  return_value={"updates": [], "error": None}),
            patch("server_watchdog.maintenance.check_failed_services",
                  return_value={"failed": ["myapp.service"], "error": None}),
            patch("server_watchdog.maintenance.check_storage",
                  return_value={"filesystems": [], "all_output": "", "threshold": 80, "error": None}),
            patch("server_watchdog.maintenance.check_journal_errors",
                  return_value={"errors": [], "error": None}),
            patch("server_watchdog.maintenance.get_hostname", return_value="host"),
        ):
            plain, html = build_report(cfg)

        assert "myapp.service" in plain
        assert "myapp.service" in html
