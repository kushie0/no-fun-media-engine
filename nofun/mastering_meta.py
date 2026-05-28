"""nofun/mastering_meta.py — Tier 1 mastering metadata.

One compute path, three sinks: a per-master sidecar JSON (source of truth), an append-only
JSONL trend log (O(1) write — no monolithic rewrite), and a one-line TUI log summary.
All skipped for trial/clip renders. Tier 1 captures only already-cheap values (levels,
alignment, recipe, a single feedback snapshot); loudness/before-after deltas are Tier 2.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import subprocess
from pathlib import Path

__all__ = [
    'SCHEMA_VERSION', 'DEAD_RMS_DB', 'CLIP_PEAK_DB', 'IMBALANCE_DB',
    'should_write_metadata', 'channel_stats', 'derive_flags', 'build_metadata',
    'write_sidecar', 'append_log', 'log_summary',
]

SCHEMA_VERSION = 1
DEAD_RMS_DB = -80.0     # mean below this = dead/silent channel
CLIP_PEAK_DB = -0.1     # peak at/above this (dBFS) = clipping
IMBALANCE_DB = 12.0     # room<->board RMS delta beyond this = flagged

_LOG_NAME = 'mastering_log.jsonl'
_sha_cache: str | None = None


def should_write_metadata(selected_only: bool, clip) -> bool:
    """Write metadata only for the real AUDIO render — never for trial/clip excerpts."""
    return bool(selected_only) and clip is None


def _pipeline_sha() -> str:
    global _sha_cache
    if _sha_cache is None:
        try:
            _sha_cache = subprocess.run(
                ['git', 'rev-parse', '--short', 'HEAD'],
                capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            _sha_cache = ''
    return _sha_cache


def channel_stats(levels_db: dict[int, tuple[float, float]],
                  align_ms: dict[int, float]) -> dict[str, dict]:
    """levels_db: {ch: (rms_db, peak_db)}. Returns per-channel dict with crest, dead, clip, align."""
    out: dict[str, dict] = {}
    for ch, (rms_db, peak_db) in sorted(levels_db.items()):
        out[str(ch)] = {
            'rms_db': round(rms_db, 1),
            'peak_db': round(peak_db, 1),
            'crest_db': round(peak_db - rms_db, 1),
            'dead': rms_db < DEAD_RMS_DB,
            'clip': peak_db >= CLIP_PEAK_DB,
            'align_ms': round(align_ms.get(ch, 0.0), 1),
        }
    return out


def derive_flags(channels: dict[str, dict], room_board: dict, peak_freqs: list[float]) -> list[str]:
    flags: list[str] = []
    for ch, s in channels.items():
        if s.get('dead'):
            flags.append(f'dead_channel:{ch}')
        if s.get('clip'):
            flags.append(f'clip:{ch}')
    if abs(room_board.get('rms_delta_db', 0.0)) >= IMBALANCE_DB:
        flags.append(f"room_board_imbalance:{room_board['rms_delta_db']:+.0f}dB")
    if peak_freqs:
        flags.append(f'feedback:{peak_freqs[0]:.0f}Hz')
    return flags


def build_metadata(performance: str, recipe: dict, channels: dict,
                   room_board: dict, feedback: dict, flags: list[str]) -> dict:
    """feedback: a prebuilt dict, e.g. {'source': 'dyers', 'peaks': [{'freq':.., 'engaged_pct':..}]}."""
    return {
        'schema_version': SCHEMA_VERSION,
        'performance': performance,
        'rendered_at': _dt.datetime.now().isoformat(timespec='seconds'),
        'pipeline_sha': _pipeline_sha(),
        'recipe': recipe,
        'channels': channels,
        'room_board': room_board,
        'feedback': feedback,
        'flags': flags,
    }


def write_sidecar(meta: dict, meta_dir: Path, base: str) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    path = meta_dir / f'{base}.json'
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(meta, indent=2), encoding='utf-8')
    tmp.replace(path)


def append_log(meta: dict, meta_dir: Path) -> None:
    """Append one compact line to the trend JSONL — O(1), never rewrites the file."""
    meta_dir.mkdir(parents=True, exist_ok=True)
    line = {
        'performance': meta['performance'],
        'rendered_at': meta['rendered_at'],
        'sha': meta['pipeline_sha'],
        'peaks': [p['freq'] for p in meta['feedback']['peaks']],
        'room_board_db': meta['room_board'].get('rms_delta_db'),
        'dead': [c for c, s in meta['channels'].items() if s.get('dead')],
        'flags': meta['flags'],
    }
    with (meta_dir / _LOG_NAME).open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(line) + '\n')


def log_summary(meta: dict, logger: logging.Logger) -> None:
    fb = meta['feedback']['peaks']
    fb_s = ', '.join(f"{p['freq']:.0f}Hz" for p in fb) if fb else 'none'
    msg = f"METADATA {meta['performance']}  feedback={fb_s}"
    if meta['flags']:
        msg += '  flags=' + ','.join(meta['flags'])
    logger.info(msg, extra={'tui': False})
