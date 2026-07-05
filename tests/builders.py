"""Shared test data builders.

Keep this small: promote helpers only after multiple tests need the same shape.
"""

from __future__ import annotations

import io
import math
import pathlib
import struct
import wave

from nofun.inventory import PerformanceState
from nofun.video import CAM_LABELS

SHORT_DATE = '26-01-01'
LONG_DATE = '2026-01-01'


def real_wav_bytes(
    frames: int = 2400,
    rate: int = 48000,
    *,
    seed: int = 0,
) -> bytes:
    """Return a valid 16-bit mono WAV that ffmpeg/FLAC can encode."""
    freq = 220.0 + seed * 55.0
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b''.join(
            struct.pack('<h', int(12000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(frames)
        ))
    return buf.getvalue()


def real_wav(
    path: pathlib.Path,
    *,
    frames: int = 48000,
    rate: int = 48000,
    seed: int = 0,
) -> pathlib.Path:
    """Write a valid 16-bit mono WAV and return its path."""
    path.write_bytes(real_wav_bytes(frames=frames, rate=rate, seed=seed))
    return path


def real_wav32(
    path: pathlib.Path,
    *,
    frames: int = 48000,
    rate: int = 48000,
    seed: int = 0,
) -> pathlib.Path:
    """Write a 32-bit mono WAV with populated low 8 bits."""
    freq = 220.0 + seed * 55.0
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(4)
        w.setframerate(rate)
        w.writeframes(b''.join(
            struct.pack(
                '<i',
                (int(0.3 * 2**31 * math.sin(2 * math.pi * freq * i / rate))
                 & ~0xFF) | (i & 0xFF),
            )
            for i in range(frames)
        ))
    path.write_bytes(buf.getvalue())
    return path


def make_quads(root: pathlib.Path, base: str, labels=CAM_LABELS) -> list[pathlib.Path]:
    """Create empty final quad files under root."""
    paths = []
    for label in labels:
        p = root / f'{base}_{label}.mp4'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'\x00')
        paths.append(p)
    return paths


def perf_state(
    date: str = SHORT_DATE,
    band: str = 'TestBand',
    **kwargs,
) -> PerformanceState:
    """Build a PerformanceState with the direct-constructor short-date contract."""
    return PerformanceState(date=date, band=band, **kwargs)
