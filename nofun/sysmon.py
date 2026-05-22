"""nofun/sysmon.py — lightweight CPU/GPU polling for the TUI progress rows."""
from __future__ import annotations

import subprocess
import threading
import time

import psutil


# PowerShell perf-counter call is ~1 s on Windows; poll loop sleeps the rest.
_POLL_INTERVAL_S = 10.0


class SysMon:
    """Singleton polling CPU% and AMD GPU% in a daemon thread.

    `get()` returns the last observed (cpu_pct, gpu_pct) tuple. Reads are
    lock-free — the GIL makes single-attribute assignment of the tuple atomic,
    and slight CPU/GPU staleness across reads is acceptable for a display row.
    """

    _instance:  SysMon | None                  = None
    _stats:     tuple[float, float | None]     = (0.0, None)

    @classmethod
    def get(cls) -> tuple[float, float | None]:
        """Return (cpu_pct, gpu_pct_or_None). Lazy-starts the daemon thread."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._stats

    def __init__(self) -> None:
        t = threading.Thread(target=self._loop, daemon=True, name='sysmon')
        t.start()

    def _loop(self) -> None:
        while True:
            cpu = psutil.cpu_percent(interval=1.0)
            gpu = self._probe_gpu()
            type(self)._stats = (cpu, gpu)
            time.sleep(max(0.0, _POLL_INTERVAL_S - 1.0))

    @staticmethod
    def _probe_gpu() -> float | None:
        """Sum AMD GPU 3D-engine utilization via Windows Performance Counter.

        Returns None on any failure — no AMD driver, non-Windows host, missing
        counter, subprocess error, or non-numeric output.
        """
        try:
            result = subprocess.run(
                [
                    'powershell', '-NoProfile', '-NonInteractive', '-Command',
                    "(Get-Counter '\\GPU Engine(*engtype_3D)\\Utilization Percentage'"
                    " -MaxSamples 1 -ErrorAction Stop).CounterSamples"
                    " | Measure-Object CookedValue -Sum"
                    " | Select-Object -ExpandProperty Sum",
                ],
                capture_output=True, text=True, timeout=5,
            )
            val = float(result.stdout.strip())
            return min(val, 100.0)
        except Exception:
            return None
