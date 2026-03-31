# server-watchdog

A lightweight daemon for RHEL 8 that provides:

1. **Monthly maintenance reports** – checks available package updates, failed
   systemd services, recent journal errors, and filesystem storage usage; then
   mails a comprehensive summary to the configured administrator.

2. **Real-time SELinux AVC denial alerts** – monitors the system journal for
   `avc: denied` messages, passes the raw denials through a Gemini LLM
   pipeline for human-readable analysis, and sends an immediate email alert.

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| RHEL 8 / CentOS 8 | systemd, journald, auditd |
| Python ≥ 3.10 | Usually pre-installed on RHEL 8+ |
| `dnf`, `systemctl`, `journalctl` | Standard RHEL tools |
| SMTP server | Local MTA (e.g. Postfix) or external SMTP |
| Google Gemini API key | [Get one free](https://aistudio.google.com/app/apikey) |

---

## Installation

```bash
sudo bash install.sh
```

The script will:
1. Create a Python virtual environment in `/opt/server-watchdog/venv`.
2. Install Python dependencies (`google-genai`) into the venv.
3. Install the package into the venv via `pip`.
4. Symlink the entry-point scripts from the venv into `/usr/bin/`.
5. Create `/etc/server-watchdog/config.ini` from the example file.
6. Install and enable the systemd units.

After installation, **edit `/etc/server-watchdog/config.ini`** and fill in your
email settings and Gemini API key.

---

## Upgrading

If you already have server-watchdog installed and want to pick up new features
(such as `server-watchdog-send-now`), pull the latest code and re-run the
installer:

```bash
cd /path/to/server-watchdog   # wherever you cloned the repository
git pull
sudo bash install.sh
```

The installer is safe to run on an existing installation:
- The Python venv is updated in-place; your packages are upgraded.
- Any new entry-point scripts are symlinked into `/usr/bin/`.
- Your existing `/etc/server-watchdog/config.ini` is **not** overwritten.
- Systemd units are refreshed and services restarted automatically.

> **Note for users who installed an earlier version:** older releases placed
> the scripts in `/usr/local/bin/`, which is absent from root's `PATH` on RHEL
> when running commands via `sudo`.  Re-running `sudo bash install.sh` moves
> the symlinks to `/usr/bin/` and fixes this automatically.

---

## Configuration

Copy `config.ini.example` to `/etc/server-watchdog/config.ini` and adjust:

```ini
[email]
smtp_host = localhost
to_addr   = admin@example.com

[llm]
api_key = YOUR_GEMINI_API_KEY_HERE
model   = gemini-1.5-pro

[maintenance]
storage_threshold = 80   # warn when ≥ 80 % full

[avc_monitor]
batch_interval = 60      # seconds to collect denials before sending alert
```

The full list of options with documentation is in `config.ini.example`.

### Sender address

Set `from_addr` in the `[email]` section to the address that should appear in
the **From:** header of every email (e.g. `watchdog@mydomain.org`):

```ini
[email]
from_addr = watchdog@mydomain.org
```

### Using a Gmail SMTP relay (port 587 / STARTTLS)

If you have configured Postfix to relay through `smtp-relay.gmail.com` and want
server-watchdog to send directly (bypassing the local MTA), set:

```ini
[email]
smtp_host    = smtp-relay.gmail.com
smtp_port    = 587
from_addr    = sender@mydomain.org
to_addr      = admin@mydomain.org
use_starttls = true
```

> **Tip:** When routing through a local Postfix relay (i.e. `relayhost` is set
> in `/etc/postfix/main.cf`), you can keep `smtp_host = localhost` and
> `smtp_port = 25` – Postfix will forward the message through the relay
> automatically.  Only set `use_starttls = true` when connecting *directly* to
> an external SMTP server on port 587.

---

## Systemd services

| Unit | Purpose |
|------|---------|
| `server-watchdog-avc.service` | Long-running AVC monitor daemon |
| `server-watchdog-monthly.service` | One-shot maintenance report |
| `server-watchdog-monthly.timer` | Triggers the service on the 1st of each month |

```bash
# Check AVC monitor status
systemctl status server-watchdog-avc.service

# Show upcoming timer runs
systemctl list-timers server-watchdog-monthly.timer

# Run maintenance report manually
systemctl start server-watchdog-monthly.service
```

---

## On-demand report

To send a single email **right now** containing all recent AVC denials and the
current system status, run:

```bash
sudo server-watchdog-send-now
```

The command will:

1. Read all `avc: denied` messages from the last `avc_lookback_days` days of
   the systemd journal (default: 7 days).
2. Pass them to the configured LLM for analysis (if an API key is set).
3. Run the full system-status check (same as the monthly report: package
   updates, failed services, storage, journal errors).
4. Send a single email combining both sections to the configured recipient.

This is useful for verifying your email and LLM configuration and for
checking what the current state of the system looks like without waiting
for a scheduled run.

> **Tip:** The lookback window is controlled by `avc_lookback_days` in the
> `[avc_monitor]` section of `config.ini`.

---

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

---

## Architecture

```
server_watchdog/
├── config.py          – INI configuration loader
├── email_sender.py    – SMTP email helper
├── llm.py             – Gemini LLM integration (AVC analysis)
├── maintenance.py     – Monthly system checks and report builder
├── avc_monitor.py     – Real-time AVC denial watcher (daemon) + snapshot reader
└── logging_setup.py   – Shared logging configuration

scripts/
├── server-watchdog-monthly       – Entry point for monthly report
├── server-watchdog-avc-monitor   – Entry point for AVC daemon
└── server-watchdog-send-now      – Entry point for on-demand email

systemd/
├── server-watchdog-avc.service
├── server-watchdog-monthly.service
└── server-watchdog-monthly.timer
```

### AVC alert flow

```
journalctl -f ──► AVCMonitor._enqueue()
                      │
                      ▼  (batch_interval seconds)
               AVCMonitor._flush()
                      │
              ┌───────┴──────────┐
              ▼                  ▼
      analyse_avc_denials()   (raw denials)
      (Gemini LLM)                │
              │                  │
              └────────┬─────────┘
                       ▼
                  send_email()
```
