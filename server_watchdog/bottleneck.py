"""Lightweight system bottleneck detection for server-watchdog.

Reads ``/proc/stat``, ``/proc/meminfo``, ``/proc/loadavg``, and
``/proc/cpuinfo`` — no external commands required.  A short CPU sample
(default 2 s) is taken to derive meaningful busy / IO-wait percentages.
"""

import logging
import time

logger = logging.getLogger(__name__)

# ── Default thresholds (percentage) ──────────────────────────────────────────
_IOWAIT_THRESHOLD = 20.0   # IO wait % of total CPU time
_CPU_THRESHOLD    = 70.0   # CPU busy % (user + system + irq + softirq)
_MEM_THRESHOLD    = 90.0   # Memory used % (i.e. < 10 % available)
_SWAP_THRESHOLD   = 10.0   # Swap used % of total swap

_SAMPLE_INTERVAL  = 2      # seconds between the two /proc/stat reads


# ── Low-level /proc readers ───────────────────────────────────────────────────

def _read_cpu_stat():
    """Return a dict of aggregate CPU jiffie counters from ``/proc/stat``."""
    with open("/proc/stat", encoding="ascii") as fh:
        for line in fh:
            if line.startswith("cpu "):
                fields = line.split()
                return {
                    "user":    int(fields[1]),
                    "nice":    int(fields[2]),
                    "system":  int(fields[3]),
                    "idle":    int(fields[4]),
                    "iowait":  int(fields[5]) if len(fields) > 5 else 0,
                    "irq":     int(fields[6]) if len(fields) > 6 else 0,
                    "softirq": int(fields[7]) if len(fields) > 7 else 0,
                    "steal":   int(fields[8]) if len(fields) > 8 else 0,
                }
    return None


def _read_mem_info():
    """Return ``/proc/meminfo`` fields as a ``{name: kB}`` dict."""
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


def _read_load_avg():
    """Return ``(load1, load5, load15, ncpus)`` from ``/proc``."""
    with open("/proc/loadavg", encoding="ascii") as fh:
        fields = fh.read().split()
    load1  = float(fields[0])
    load5  = float(fields[1])
    load15 = float(fields[2])

    ncpus = 0
    with open("/proc/cpuinfo", encoding="ascii") as fh:
        for line in fh:
            if line.startswith("processor"):
                ncpus += 1
    return load1, load5, load15, max(ncpus, 1)


# ── Public API ────────────────────────────────────────────────────────────────

def check_bottlenecks(sample_interval=_SAMPLE_INTERVAL):
    """Sample CPU, IO wait, memory, and load; return a bottleneck summary.

    Takes two ``/proc/stat`` snapshots *sample_interval* seconds apart so that
    CPU-busy and IO-wait percentages are measured over an actual time window
    rather than since boot.  Memory metrics are instantaneous.

    Parameters
    ----------
    sample_interval:
        Seconds to wait between the two CPU samples.

    Returns
    -------
    dict
        ``{'bottlenecks': [{'name': str, 'pct': float}, ...],
           'detail': dict, 'error': None|str}``

        Active bottlenecks are those above their respective thresholds,
        sorted by percentage descending.  ``detail`` contains the raw
        per-metric values for logging / LLM context.
    """
    try:
        s1 = _read_cpu_stat()
        time.sleep(sample_interval)
        s2 = _read_cpu_stat()

        if s1 is None or s2 is None:
            return {
                "bottlenecks": [], "detail": {},
                "error": "/proc/stat not available",
            }

        # ── CPU deltas ────────────────────────────────────────────────────
        d = {k: s2[k] - s1[k] for k in s1}
        total = sum(d.values())
        if total > 0:
            cpu_busy_pct = (
                d["user"] + d["nice"] + d["system"] + d["irq"] + d["softirq"]
            ) / total * 100
            iowait_pct = d["iowait"] / total * 100
        else:
            cpu_busy_pct = iowait_pct = 0.0

        # ── Memory ────────────────────────────────────────────────────────
        mem           = _read_mem_info()
        mem_total     = mem.get("MemTotal", 0)
        mem_available = mem.get("MemAvailable", mem.get("MemFree", 0))
        swap_total    = mem.get("SwapTotal", 0)
        swap_free     = mem.get("SwapFree", 0)

        mem_used_pct  = (
            (mem_total - mem_available) / mem_total * 100 if mem_total else 0.0
        )
        swap_used_pct = (
            (swap_total - swap_free) / swap_total * 100 if swap_total else 0.0
        )

        # ── Load average ──────────────────────────────────────────────────
        load1, load5, load15, ncpus = _read_load_avg()

        detail = {
            "cpu_pct":       round(cpu_busy_pct, 1),
            "iowait_pct":    round(iowait_pct, 1),
            "mem_used_pct":  round(mem_used_pct, 1),
            "swap_used_pct": round(swap_used_pct, 1),
            "load1":         load1,
            "load5":         load5,
            "load15":        load15,
            "ncpus":         ncpus,
        }

        # ── Active bottlenecks ────────────────────────────────────────────
        candidates = [
            ("IO wait", iowait_pct,    _IOWAIT_THRESHOLD),
            ("CPU",     cpu_busy_pct,  _CPU_THRESHOLD),
            ("Memory",  mem_used_pct,  _MEM_THRESHOLD),
            ("Swap",    swap_used_pct, _SWAP_THRESHOLD),
        ]
        bottlenecks = [
            {"name": name, "pct": round(pct, 1)}
            for name, pct, threshold in sorted(
                candidates, key=lambda x: x[1], reverse=True
            )
            if pct >= threshold
        ]

        return {"bottlenecks": bottlenecks, "detail": detail, "error": None}

    except OSError as exc:
        logger.warning("Bottleneck check failed: %s", exc)
        return {"bottlenecks": [], "detail": {}, "error": str(exc)}
