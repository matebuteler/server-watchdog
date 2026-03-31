"""Configuration handling for server-watchdog."""

import configparser
import os

DEFAULT_CONFIG_PATH = "/etc/server-watchdog/config.ini"


class Config:
    """Load and expose server-watchdog configuration."""

    def __init__(self, config_path=None):
        self._config_path = config_path or os.environ.get(
            "WATCHDOG_CONFIG", DEFAULT_CONFIG_PATH
        )
        self._parser = configparser.ConfigParser()
        self._apply_defaults()
        self._load()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_defaults(self):
        defaults = {
            "email": {
                "smtp_host": "localhost",
                "smtp_port": "25",
                "from_addr": "watchdog@localhost",
                "to_addr": "root@localhost",
                "use_tls": "false",
                "use_starttls": "false",
                "username": "",
                "password": "",
                "subject_prefix": "[server-watchdog]",
            },
            "llm": {
                "provider": "gemini",
                "api_key": "",
                "model": "gemini-1.5-pro",
            },
            "maintenance": {
                "check_packages": "true",
                "check_services": "true",
                "check_storage": "true",
                "storage_threshold": "80",
                "log_lookback_days": "30",
                "coredump_age_days": "45",
            },
            "avc_monitor": {
                "batch_interval": "60",
                "avc_lookback_days": "7",
            },
            "logging": {
                "log_file": "/var/log/server-watchdog/watchdog.log",
                "level": "INFO",
            },
            "server": {
                "context": "Linux server",
                "uid_map": "",
            },
        }
        for section, options in defaults.items():
            self._parser.add_section(section)
            for key, value in options.items():
                self._parser.set(section, key, value)

    def _load(self):
        if os.path.exists(self._config_path):
            self._parser.read(self._config_path)

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def get(self, section, key, fallback=None):
        return self._parser.get(section, key, fallback=fallback)

    def getint(self, section, key, fallback=None):
        return self._parser.getint(section, key, fallback=fallback)

    def getboolean(self, section, key, fallback=None):
        return self._parser.getboolean(section, key, fallback=fallback)
