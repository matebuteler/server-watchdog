"""Tests for server_watchdog.maintenance."""

import subprocess
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from server_watchdog.config import Config
from server_watchdog.maintenance import (
    build_report,
    check_coredumps,
    check_failed_services,
    check_journal_errors,
    check_packages,
    check_storage,
    get_service_logs,
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


# ---------------------------------------------------------------------------
# check_coredumps
# ---------------------------------------------------------------------------

class TestCheckCoredumps:
    def test_coredumpctl_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            data = check_coredumps()
        assert data["dumps"] == []
        assert "not found" in data["error"]

    def test_coredumpctl_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("coredumpctl", 30)):
            data = check_coredumps()
        assert data["dumps"] == []
        assert "timed out" in data["error"]

    def test_no_coredumps(self):
        with patch("subprocess.run", return_value=_completed(0, "")):
            data = check_coredumps()
        assert data["dumps"] == []
        assert data["error"] is None

    def test_recent_dump_included(self):
        recent_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        output = f"Mon {recent_date} 12:00:00 UTC  1234  1000  1000 SIGSEGV present  /usr/bin/myapp  56K\n"
        with patch("subprocess.run", return_value=_completed(0, output)):
            data = check_coredumps(max_age_days=45)
        assert len(data["dumps"]) == 1
        assert "/usr/bin/myapp" in data["dumps"][0]

    def test_old_dump_excluded(self):
        old_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        output = f"Mon {old_date} 12:00:00 UTC  1234  1000  1000 SIGSEGV present  /usr/bin/myapp  56K\n"
        with patch("subprocess.run", return_value=_completed(0, output)):
            data = check_coredumps(max_age_days=45)
        assert data["dumps"] == []

    def test_unparseable_line_included_conservatively(self):
        output = "not-a-valid-coredump-line\n"
        with patch("subprocess.run", return_value=_completed(0, output)):
            data = check_coredumps(max_age_days=45)
        # Lines that can't be date-parsed are included conservatively
        assert len(data["dumps"]) == 1

    def test_exit_code_1_treated_as_empty(self):
        # Some distros return exit code 1 when there are no coredumps
        with patch("subprocess.run", return_value=_completed(1, "")):
            data = check_coredumps()
        assert data["dumps"] == []
        assert data["error"] is None


# ---------------------------------------------------------------------------
# get_service_logs
# ---------------------------------------------------------------------------

class TestGetServiceLogs:
    def test_returns_output(self):
        log_output = "2026-01-01T10:00:00+0000 host myapp[123]: ERROR: something failed"
        with patch("subprocess.run", return_value=_completed(0, log_output)):
            result = get_service_logs("myapp.service")
        assert "ERROR" in result

    def test_journalctl_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = get_service_logs("myapp.service")
        assert "not found" in result

    def test_journalctl_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("journalctl", 30)):
            result = get_service_logs("myapp.service")
        assert "timed out" in result


# ---------------------------------------------------------------------------
# check_storage – NFS separation
# ---------------------------------------------------------------------------

class TestCheckStorageNFS:
    _DF_OUTPUT_WITH_NFS = (
        "Filesystem     Type   Size  Used Avail Use% Mounted on\n"
        "/dev/sda1      xfs    20G   16G  4G    82%  /\n"
        "server:/exp    nfs4   100G  96G  4G    96%  /mnt/nfs\n"
        "/dev/sda2      xfs    10G    1G  9G    10%  /boot\n"
    )

    def test_nfs_filesystem_in_nfs_list(self):
        with patch("subprocess.run", return_value=_completed(0, self._DF_OUTPUT_WITH_NFS)):
            data = check_storage(threshold=80)
        assert any("nfs" in line.lower() or "/mnt/nfs" in line
                   for line in data["nfs_filesystems"])

    def test_local_fs_not_in_nfs_list(self):
        with patch("subprocess.run", return_value=_completed(0, self._DF_OUTPUT_WITH_NFS)):
            data = check_storage(threshold=80)
        assert any("/dev/sda1" in line for line in data["filesystems"])
        assert not any("/dev/sda1" in line for line in data["nfs_filesystems"])

    def test_nfs_not_in_local_list(self):
        with patch("subprocess.run", return_value=_completed(0, self._DF_OUTPUT_WITH_NFS)):
            data = check_storage(threshold=80)
        assert not any("/mnt/nfs" in line for line in data["filesystems"])

    def test_nfs_key_present_when_no_nfs(self):
        df_out = (
            "Filesystem     Type   Size  Used Avail Use% Mounted on\n"
            "/dev/sda1      xfs    20G   16G  4G    82%  /\n"
        )
        with patch("subprocess.run", return_value=_completed(0, df_out)):
            data = check_storage(threshold=80)
        assert "nfs_filesystems" in data
        assert data["nfs_filesystems"] == []




# ---------------------------------------------------------------------------
# build_report – LLM path
# ---------------------------------------------------------------------------

class TestBuildReportLLM:
    _EMPTY_PACKAGES = {"updates": [], "error": None}
    _EMPTY_SERVICES = {"failed": [], "error": None}
    _EMPTY_STORAGE = {
        "filesystems": [], "nfs_filesystems": [],
        "all_output": "", "threshold": 80, "error": None,
    }
    _EMPTY_JOURNAL = {"errors": [], "error": None}
    _EMPTY_COREDUMPS = {"dumps": [], "error": None}

    def test_llm_called_when_api_key_set(self):
        cfg = _make_config(**{"llm.api_key": "fake-key"})
        with patch("server_watchdog.maintenance.check_packages",
                   return_value=self._EMPTY_PACKAGES), \
             patch("server_watchdog.maintenance.check_failed_services",
                   return_value=self._EMPTY_SERVICES), \
             patch("server_watchdog.maintenance.check_storage",
                   return_value=self._EMPTY_STORAGE), \
             patch("server_watchdog.maintenance.check_journal_errors",
                   return_value=self._EMPTY_JOURNAL), \
             patch("server_watchdog.maintenance.check_coredumps",
                   return_value=self._EMPTY_COREDUMPS), \
             patch("server_watchdog.maintenance.get_hostname", return_value="llmhost"), \
             patch("server_watchdog.llm.analyse_maintenance_report",
                   return_value="## Healthy\n\nAll good.") as mock_llm:
            plain, html = build_report(cfg)

        mock_llm.assert_called_once()
        assert "Healthy" in plain
        assert "Healthy" in html

    def test_llm_not_called_without_api_key(self):
        cfg = _make_config(**{"llm.api_key": ""})
        with patch("server_watchdog.maintenance.check_packages",
                   return_value=self._EMPTY_PACKAGES), \
             patch("server_watchdog.maintenance.check_failed_services",
                   return_value=self._EMPTY_SERVICES), \
             patch("server_watchdog.maintenance.check_storage",
                   return_value=self._EMPTY_STORAGE), \
             patch("server_watchdog.maintenance.check_journal_errors",
                   return_value=self._EMPTY_JOURNAL), \
             patch("server_watchdog.maintenance.check_coredumps",
                   return_value=self._EMPTY_COREDUMPS), \
             patch("server_watchdog.maintenance.get_hostname", return_value="host"), \
             patch("server_watchdog.llm.analyse_maintenance_report") as mock_llm:
            build_report(cfg)

        mock_llm.assert_not_called()

    def test_llm_failure_falls_back_to_static(self):
        cfg = _make_config(**{"llm.api_key": "fake-key"})
        with patch("server_watchdog.maintenance.check_packages",
                   return_value=self._EMPTY_PACKAGES), \
             patch("server_watchdog.maintenance.check_failed_services",
                   return_value=self._EMPTY_SERVICES), \
             patch("server_watchdog.maintenance.check_storage",
                   return_value=self._EMPTY_STORAGE), \
             patch("server_watchdog.maintenance.check_journal_errors",
                   return_value=self._EMPTY_JOURNAL), \
             patch("server_watchdog.maintenance.check_coredumps",
                   return_value=self._EMPTY_COREDUMPS), \
             patch("server_watchdog.maintenance.get_hostname", return_value="llmhost"), \
             patch("server_watchdog.llm.analyse_maintenance_report",
                   return_value="(LLM analysis failed: quota exceeded)"):
            plain, html = build_report(cfg)

        # Falls back to static report which includes the hostname
        assert "llmhost" in plain

    def test_coredumps_in_static_report(self):
        cfg = _make_config()
        with patch("server_watchdog.maintenance.check_packages",
                   return_value=self._EMPTY_PACKAGES), \
             patch("server_watchdog.maintenance.check_failed_services",
                   return_value=self._EMPTY_SERVICES), \
             patch("server_watchdog.maintenance.check_storage",
                   return_value=self._EMPTY_STORAGE), \
             patch("server_watchdog.maintenance.check_journal_errors",
                   return_value=self._EMPTY_JOURNAL), \
             patch("server_watchdog.maintenance.check_coredumps",
                   return_value={"dumps": ["Mon 2026-01-01 10:00:00 UTC 42 1000 SIGSEGV /usr/bin/crash"], "error": None}), \
             patch("server_watchdog.maintenance.get_hostname", return_value="host"):
            plain, html = build_report(cfg)

        assert "crash" in plain
        assert "crash" in html
