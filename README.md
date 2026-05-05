# server-watchdog

A lightweight daemon for RHEL 8+ and openSUSE that provides:

1. **Monthly maintenance reports** – checks available package updates (dnf/zypper),
   failed systemd services, recent journal errors, and filesystem storage usage;
   then mails a comprehensive summary to the configured administrator.

2. **Real-time MAC denial alerts** – monitors the system journal for
   SELinux `avc: denied` messages (RHEL) or AppArmor `DENIED` events (openSUSE),
   passes the raw denials through a Gemini LLM pipeline with search grounding
   for human-readable analysis, and sends an immediate email alert.

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| RHEL 8+ / CentOS / openSUSE | systemd, journald |
| Python ≥ 3.10 | Usually pre-installed |
| `dnf` or `zypper`, `systemctl`, `journalctl` | Standard distro tools |
| SMTP server **or** msmtp | Local MTA, external SMTP, or personal `~/.msmtprc` |
| Google Gemini API key | [Get one free](https://aistudio.google.com/app/apikey) |

---

## Installation

```bash
sudo bash install.sh
```

The script will:
1. Auto-detect your distribution (RHEL, openSUSE, etc.).
2. Create a Python virtual environment in `/opt/server-watchdog/venv`.
3. Install Python dependencies (`google-genai`) into the venv.
4. Install the package into the venv via `pip`.
5. Symlink the entry-point scripts from the venv into `/usr/bin/`.
6. Create `/etc/server-watchdog/config.ini` from the example file.
7. Install and enable the systemd units.

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
model   = gemini-3-flash-preview
```

The full list of options with documentation is in `config.ini.example`.

### Sender address

Set `from_addr` in the `[email]` section to the address that should appear in
the **From:** header of every email (e.g. `watchdog@mydomain.org`):

```ini
[email]
from_addr = watchdog@mydomain.org
```

### Using msmtp (personal workspace accounts)

If you already have a working `~/.msmtprc` (e.g. Gmail with App Passwords),
you can reuse it instead of configuring SMTP in `config.ini`:

```ini
[email]
backend       = msmtp
to_addr       = admin@example.com
from_addr     = your@gmail.com
msmtp_account = gmail
```

Example `~/.msmtprc` for Gmail:
```
defaults
auth           on
tls            on
tls_trust_file /etc/pki/tls/certs/ca-bundle.crt      # RHEL/CentOS/Fedora
# tls_trust_file /etc/ssl/ca-bundle.pem               # openSUSE/SLES
# tls_trust_file /etc/ssl/certs/ca-certificates.crt   # Debian/Ubuntu
logfile        ~/.msmtp.log

account        gmail
host           smtp.gmail.com
port           587
from           your@gmail.com
user           your@gmail.com
password       your-app-password

account default : gmail
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

### Rate limiting and model fallback

The Gemini free tier has strict rate limits.  server-watchdog tracks usage
per model and cascades through a fallback chain when limits are hit:

| Priority | Model | Intelligence (AAII) | RPM | RPD |
|----------|-------|---------------------|-----|-----|
| Primary | `gemini-3-flash-preview` | 46 | 5 | 20 |
| Fallback 1 | `gemma-4-31b-it` | 39 | 15 | 1,500 |
| Fallback 2 | `gemini-3.1-flash-lite-preview` | 34 | 15 | 500 |

Use `--no-fallback` on the CLI to wait instead of falling back:

```bash
sudo server-watchdog-send-now --no-fallback
```

### Search grounding (3-step pipeline)

When `search_grounding = true` (default), analysis uses a 3-step pipeline:
1. Primary model produces initial analysis.
2. `gemini-2.5-flash` validates/enriches it via Google Search (CVEs, advisories).
3. Primary model refines the analysis with grounded real-time context.

This doubles primary model usage (~10 analyses/day at 20 RPD).
Set `search_grounding = false` to disable and conserve rate budget.

---

## Systemd services

| Unit | Purpose |
|------|---------|
| `server-watchdog-avc.service` | Long-running denial monitor daemon |
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

To send a single email **right now** containing all recent denials and the
current system status, run:

```bash
sudo server-watchdog-send-now
```

The command will:

1. Read all `avc: denied` (SELinux) or `apparmor="DENIED"` (AppArmor)
   messages from the last `avc_lookback_days` days of the systemd journal
   (default: 7 days).
2. Pass them to the configured LLM for analysis (with search grounding
   if enabled).
3. Run the full system-status check (same as the monthly report: package
   updates, failed services, storage, journal errors).
4. Send a single email combining both sections to the configured recipient.

Options:
- `--no-fallback`: Wait for rate limits instead of cascading to lower models.

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
├── email_sender.py    – SMTP / msmtp email helper
├── llm.py             – Gemini LLM integration (analysis + search grounding)
├── rate_limiter.py    – Per-model rate limiting with cascading fallback
├── maintenance.py     – Monthly system checks and report builder (dnf/zypper)
├── avc_monitor.py     – Real-time denial watcher (SELinux + AppArmor)
├── utils.py           – Shared helpers (distro detection, MAC detection, etc.)
└── logging_setup.py   – Shared logging configuration

scripts/
├── server-watchdog-monthly       – Entry point for monthly report
├── server-watchdog-avc-monitor   – Entry point for denial daemon
└── server-watchdog-send-now      – Entry point for on-demand email

systemd/
├── server-watchdog-avc.service
├── server-watchdog-monthly.service
└── server-watchdog-monthly.timer
```

### Analysis pipeline (with search grounding)

```
 Raw denials / maintenance data
         │
         ▼
 ┌──────────────────────┐
 │  Step 1: G3 Flash    │  Initial analysis (ungrounded)
 │  (AAII 46, primary)  │
 └──────────┬───────────┘
            ▼
 ┌──────────────────────┐
 │  Step 2: G2.5 Flash  │  Search grounding via Google Search
 │  + Google Search     │  (CVEs, advisories, known issues)
 └──────────┬───────────┘
            ▼
 ┌──────────────────────┐
 │  Step 3: G3 Flash    │  Refined analysis with grounded context
 │  (AAII 46, primary)  │
 └──────────┬───────────┘
            ▼
       send_email()
```

### Denial alert flow

```
journalctl -f ──► _is_mac_denial()
                       │
                       ▼  (batch_interval seconds)
                AVCMonitor._flush()
                       │
               ┌───────┴──────────┐
               ▼                  ▼
      analyse_avc_denials()   (raw denials)
      (3-step pipeline)           │
               │                  │
               └────────┬─────────┘
                        ▼
                   send_email()
```
