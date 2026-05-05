"""Tests for distro and MAC system detection."""

from unittest.mock import patch, mock_open

import pytest

from server_watchdog.utils import detect_distro, detect_mac_system


class TestDetectDistro:
    def test_rhel(self):
        content = 'NAME="Red Hat Enterprise Linux"\nID=rhel\nVERSION_ID="8.9"\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "rhel"

    def test_centos(self):
        content = 'NAME="CentOS Stream"\nID=centos\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "centos"

    def test_opensuse_leap(self):
        content = 'NAME="openSUSE Leap"\nID=opensuse-leap\nVERSION_ID="15.5"\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "opensuse-leap"

    def test_opensuse_tumbleweed(self):
        content = 'NAME="openSUSE Tumbleweed"\nID=opensuse-tumbleweed\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "opensuse-tumbleweed"

    def test_sles(self):
        content = 'NAME="SLES"\nID="sles"\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "sles"

    def test_ubuntu(self):
        content = 'NAME="Ubuntu"\nID=ubuntu\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "ubuntu"

    def test_quoted_id(self):
        content = 'ID="fedora"\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "fedora"

    def test_missing_file_returns_unknown(self):
        with patch("builtins.open", side_effect=OSError):
            assert detect_distro() == "unknown"

    def test_no_id_field_returns_unknown(self):
        content = 'NAME="Some Linux"\nVERSION="1.0"\n'
        with patch("builtins.open", mock_open(read_data=content)):
            assert detect_distro() == "unknown"


class TestDetectMacSystem:
    def test_apparmor_detected(self):
        with patch("os.path.isdir", return_value=True):
            assert detect_mac_system() == "apparmor"

    def test_selinux_via_sysfs(self):
        def isdir(path):
            return False  # no AppArmor

        def isfile(path):
            return path == "/sys/fs/selinux/enforce"

        with patch("os.path.isdir", side_effect=isdir), \
             patch("os.path.isfile", side_effect=isfile):
            assert detect_mac_system() == "selinux"

    def test_selinux_via_sestatus(self):
        def isdir(path):
            return False

        def isfile(path):
            return False

        mock_result = type("Result", (), {"stdout": "SELinux status: enabled\n"})()

        with patch("os.path.isdir", side_effect=isdir), \
             patch("os.path.isfile", side_effect=isfile), \
             patch("subprocess.run", return_value=mock_result):
            assert detect_mac_system() == "selinux"

    def test_none_when_nothing_found(self):
        def isdir(path):
            return False

        def isfile(path):
            return False

        with patch("os.path.isdir", side_effect=isdir), \
             patch("os.path.isfile", side_effect=isfile), \
             patch("subprocess.run", side_effect=FileNotFoundError):
            assert detect_mac_system() == "none"
