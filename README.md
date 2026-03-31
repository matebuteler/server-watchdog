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
1. Install Python dependencies (`google-genai`).
2. Install the package via `pip`.
3. Create `/etc/server-watchdog/config.ini` from the example file.
4. Install and enable the systemd units.

After installation, **edit `/etc/server-watchdog/config.ini`** and fill in your
email settings and Gemini API key.

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

## Development

```bash
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
├── avc_monitor.py     – Real-time AVC denial watcher (daemon)
└── logging_setup.py   – Shared logging configuration

scripts/
├── server-watchdog-monthly       – Entry point for monthly report
└── server-watchdog-avc-monitor   – Entry point for AVC daemon

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
