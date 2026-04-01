"""Bottleneck detection for server-watchdog.

Architecture
------------
A lightweight **sampler** (``server-watchdog-sampler``, driven by a systemd
timer every 5 minutes) reads ``/proc/stat``, ``/proc/meminfo``, and
``/proc/loadavg`` and appends a compact JSON record to a persistent JSONL
data file.

At report time the **analyser** (``check_bottlenecks``) reads that file,
filters to the configured lookback window, and computes a **performance
impact score** for each bottleneck type.  The score (0–100) combines three
factors:

* **Severity** — how far above the threshold the metric was when active,
  normalised to the 0–1 range above the threshold.
* **Frequency** — how often the metric was above its threshold across all
  samples in the window.
* **VNC impact weight** — a per-metric constant reflecting how much that
  resource type degrades the experience of interactive (VNC) users.

  IO wait and Swap score highest (1.0) because latency spikes make VNC
  sessions freeze outright.  CPU scores 0.9 (slows encoding / rendering).
  Memory pressure scores 0.6 (indirect effect until swap kicks in).

Only bottlenecks whose impact score reaches ``_MIN_IMPACT_SCORE`` (default
5 %) are included in the report.

No external commands are needed — all data comes from ``/proc``.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_DATA_FILE       = "/var/log/server-watchdog/bottleneck.jsonl"
DEFAULT_LOOKBACK_DAYS   = 14
DEFAULT_SAMPLE_INTERVAL = 2   # seconds between the two /proc/stat reads

# Thresholds (%) — a metric must exceed its threshold to contribute to impact
_IOWAIT_THRESHOLD = 20.0
_CPU_THRESHOLD    = 70.0
_MEM_THRESHOLD    = 90.0
_SWAP_THRESHOLD   = 10.0

# VNC-relevance weights (0–1): how much each bottleneck type hurts interactive users
_VNC_WEIGHTS = {
    "IO wait": 1.0,   # stalls block VNC sessions entirely
    "CPU":     0.9,   # slows frame encoding and application response
    "Memory":  0.6,   # indirect until swap begins
    "Swap":    1.0,   # swap I/O causes severe latency spikes
}

# Minimum impact score (0–100) required to include a bottleneck in the report
_MIN_IMPACT_SCORE = 5.0


# ── Low-level /proc readers ───────────────────────────────────────────────────

def _read_cpu_stat():
    """Return aggregate CPU jiffie counters from ``/proc/stat``, or ``None``."""
    with open("/proc/stat", encoding="ascii") as fh:
        for line in fh:
            if line.startswith("cpu "):
                f = line.split()
                return {
                    "user":    int(f[1]),
                    "nice":    int(f[2]),
                    "system":  int(f[3]),
                    "idle":    int(f[4]),
                    "iowait":  int(f[5]) if len(f) > 5 else 0,
                    "irq":     int(f[6]) if len(f) > 6 else 0,
                    "softirq": int(f[7]) if len(f) > 7 else 0,
                    "steal":   int(f[8]) if len(f) > 8 else 0,
                }
    return None


def _read_mem_info():
    """Return ``/proc/meminfo`` key fields as a ``{name: kB_int}`` dict."""
    info = {}
    with open("/proc/meminfo", encoding="ascii") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    info[parts[0].rstrip(":")] = int(parts[1])
                except ValueError:
                    pass
    return info


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _sample_impact(metric_pct, threshold, vnc_weight):
    """Return the per-sample impact contribution of one metric (0–1).

    Zero when the metric is below *threshold*.  Above the threshold the
    contribution rises linearly with how far the metric exceeds the threshold,
    normalised to the remaining headroom ``(100 - threshold)``, then scaled
    by *vnc_weight*.
    """
    if metric_pct < threshold:
        return 0.0
    excess = min(metric_pct - threshold, 100.0 - threshold)
    return (excess / (100.0 - threshold)) * vnc_weight


# ── Sampler (called by server-watchdog-sampler every N minutes) ───────────────

def take_sample(data_file=DEFAULT_DATA_FILE,
                sample_interval=DEFAULT_SAMPLE_INTERVAL):
    """Record one CPU/IO/memory snapshot, appending it as a JSONL line.

    Two ``/proc/stat`` reads *sample_interval* seconds apart are used so that
    CPU percentages reflect the measurement window rather than uptime.

    Parameters
    ----------
    data_file:
        Path to the JSONL file (created automatically if absent).
    sample_interval:
        Seconds between the two ``/proc/stat`` reads.
    """
    try:
        s1 = _read_cpu_stat()
        time.sleep(sample_interval)
        s2 = _read_cpu_stat()

        if s1 is None or s2 is None:
            logger.warning("Could not read /proc/stat; skipping sample.")
            return

        d     = {k: s2[k] - s1[k] for k in s1}
        total = sum(d.values())
        if total > 0:
            cpu_pct    = (d["user"] + d["nice"] + d["system"]
                          + d["irq"] + d["softirq"]) / total * 100
            iowait_pct = d["iowait"] / total * 100
        else:
            cpu_pct = iowait_pct = 0.0

        mem           = _read_mem_info()
        mem_total     = mem.get("MemTotal", 0)
        mem_available = mem.get("MemAvailable", mem.get("MemFree", 0))
        swap_total    = mem.get("SwapTotal", 0)
        swap_free     = mem.get("SwapFree", 0)

        mem_pct  = (mem_total - mem_available) / mem_total * 100 if mem_total  else 0.0
        swap_pct = (swap_total - swap_free)    / swap_total * 100 if swap_total else 0.0

        record = {
            "ts":     int(time.time()),
            "cpu":    round(cpu_pct,    1),
            "iowait": round(iowait_pct, 1),
            "mem":    round(mem_pct,    1),
            "swap":   round(swap_pct,   1),
        }

        Path(data_file).parent.mkdir(parents=True, exist_ok=True)
        with open(data_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

        logger.debug("Bottleneck sample recorded: %s", record)

    except OSError as exc:
        logger.error("Failed to record bottleneck sample: %s", exc)


# ── Analyser (called at report time) ─────────────────────────────────────────

def analyze_bottlenecks(data_file=DEFAULT_DATA_FILE,
                        lookback_days=DEFAULT_LOOKBACK_DAYS):
    """Read *data_file* and return a performance-impact bottleneck summary.

    For each sample in the lookback window, a per-metric impact contribution
    is computed via :func:`_sample_impact`.  Contributions are averaged across
    all samples to produce an **impact score** (0–100) for each bottleneck
    type.  Only types whose score reaches ``_MIN_IMPACT_SCORE`` appear in the
    result, sorted by score descending.

    Parameters
    ----------
    data_file:
        Path to the JSONL file written by :func:`take_sample`.
    lookback_days:
        Only samples newer than this many days are considered.

    Returns
    -------
    dict
        ``{'bottlenecks': [{'name': str, 'score': float}, ...],
           'total_samples': int,
           'lookback_days': int,
           'error': None|str}``

        ``score`` is the performance impact score (0–100): a higher value
        means the bottleneck more severely degraded user experience over the
        lookback period.
    """
    cutoff_ts = int((datetime.now() - timedelta(days=lookback_days)).timestamp())

    data_path = Path(data_file)
    if not data_path.exists():
        return {
            "bottlenecks": [], "total_samples": 0,
            "lookback_days": lookback_days,
            "error": (
                "No sample data yet. "
                "Ensure server-watchdog-sampler.timer is enabled."
            ),
        }

    try:
        samples = []
        with open(data_path, encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    s = json.loads(raw)
                    if s.get("ts", 0) >= cutoff_ts:
                        samples.append(s)
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping malformed line %d in %s", line_no, data_file
                    )

        if not samples:
            return {
                "bottlenecks": [], "total_samples": 0,
                "lookback_days": lookback_days,
                "error": f"No samples in the last {lookback_days} days.",
            }

        # Accumulate per-metric impact across all samples
        impact_sums = {name: 0.0 for name in _VNC_WEIGHTS}
        thresholds = {
            "IO wait": _IOWAIT_THRESHOLD,
            "CPU":     _CPU_THRESHOLD,
            "Memory":  _MEM_THRESHOLD,
            "Swap":    _SWAP_THRESHOLD,
        }
        metric_keys = {
            "IO wait": "iowait",
            "CPU":     "cpu",
            "Memory":  "mem",
            "Swap":    "swap",
        }

        for s in samples:
            for name, weight in _VNC_WEIGHTS.items():
                impact_sums[name] += _sample_impact(
                    s.get(metric_keys[name], 0.0),
                    thresholds[name],
                    weight,
                )

        n = len(samples)
        bottlenecks = []
        for name in sorted(impact_sums, key=impact_sums.__getitem__, reverse=True):
            score = round(impact_sums[name] / n * 100, 1)
            if score >= _MIN_IMPACT_SCORE:
                bottlenecks.append({"name": name, "score": score})

        return {
            "bottlenecks": bottlenecks,
            "total_samples": n,
            "lookback_days": lookback_days,
            "error": None,
        }

    except OSError as exc:
        logger.error("Failed to analyze bottleneck data: %s", exc)
        return {
            "bottlenecks": [], "total_samples": 0,
            "lookback_days": lookback_days,
            "error": str(exc),
        }


# ── Public API for maintenance.py ─────────────────────────────────────────────

def check_bottlenecks(config):
    """Read config and return the historical bottleneck analysis.

    Returns ``None`` when the check is disabled in config, otherwise the dict
    returned by :func:`analyze_bottlenecks`.
    """
    if not config.getboolean("maintenance", "check_bottlenecks", fallback=True):
        return None

    data_file = config.get(
        "maintenance", "bottleneck_data_file", fallback=DEFAULT_DATA_FILE
    )
    lookback_days = config.getint(
        "maintenance", "bottleneck_lookback_days", fallback=DEFAULT_LOOKBACK_DAYS
    )
    return analyze_bottlenecks(data_file=data_file, lookback_days=lookback_days)

