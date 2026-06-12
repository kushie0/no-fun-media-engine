#!/usr/bin/env python3
"""Standalone read-only NAS deliverable integrity audit (audio v1).

Walks the NAS audio deliverables (``*_MULTITRACK.zip``) and reports a per-performance
integrity verdict. Read-only: it never deletes, moves, or rewrites anything.

Fast tier (default): existence + size + readable central directory + channel count, plus
cross-tier parity against the D: backup zip and channel-expectation against D: raw WAVs.
Deep tier (``--deep``): adds ``testzip()`` CRC over every member and an ffmpeg decode-null
on each FLAC member.

See docs/active/2026-06_nas-integrity-audit.md for the plan and verdict semantics.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from nofun.inventory import extract_date_band, perf_key  # noqa: E402

HARD_FAIL = {'MISSING', 'ZERO_BYTES', 'UNOPENABLE', 'CRC_FAIL', 'DECODE_FAIL', 'CHANNEL_INCOMPLETE'}


@dataclass
class PerfRecord:
    pk: str
    nas_zip: pathlib.Path | None = None
    d_zip: pathlib.Path | None = None
    d_wavs: list[pathlib.Path] = field(default_factory=list)


@dataclass
class Probe:
    nas_size: int = 0
    d_size: int | None = None
    entries: int = 0
    chans: int = 0
    openable: bool = False
    error: str = ''
    deep: bool = False
    crc_bad: str | None = None
    decode_bad: str | None = None


def build_perfs(nas_audio: pathlib.Path,
                d_zip: pathlib.Path | None,
                d_archive: pathlib.Path | None) -> dict[str, PerfRecord]:
    """Union every performance visible across NAS deliverables, D: backup zips, and
    D: raw WAVs — so nothing is silently skipped the way ``_zip_verified`` skips perfs
    not in the D: zip-stem set."""
    perfs: dict[str, PerfRecord] = {}

    def rec(pk: str) -> PerfRecord:
        return perfs.setdefault(pk, PerfRecord(pk))

    for z in sorted(nas_audio.glob('*_MULTITRACK.zip')):
        rec(z.stem.removesuffix('_MULTITRACK')).nas_zip = z
    if d_zip:
        for z in sorted(d_zip.glob('*_MULTITRACK.zip')):
            rec(z.stem.removesuffix('_MULTITRACK')).d_zip = z
    if d_archive:
        for w in sorted(d_archive.glob('*.wav')):
            date_str, band = extract_date_band(w.stem)
            rec(perf_key(date_str, band)).d_wavs.append(w)
    return perfs


def _decode_check(zf: zipfile.ZipFile, names: list[str]) -> str | None:
    """Return the first FLAC member that ffmpeg cannot decode cleanly, or None."""
    for n in names:
        if not n.lower().endswith('.flac'):
            continue
        try:
            data = zf.read(n)
        except (zipfile.BadZipFile, OSError) as e:
            return f'{n} (read failed: {e})'
        r = subprocess.run(
            ['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-f', 'null', '-'],
            input=data, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if r.returncode != 0 or r.stderr.strip():
            return n
    return None


def probe_perf(rec: PerfRecord, deep: bool) -> Probe:
    """Read-only disk probe of one performance's NAS zip (+ D: backup size)."""
    p = Probe()
    if rec.d_zip is not None:
        try:
            p.d_size = rec.d_zip.stat().st_size
        except OSError:
            p.d_size = None
    if rec.nas_zip is None:
        return p
    try:
        p.nas_size = rec.nas_zip.stat().st_size
    except OSError as e:
        p.error = f'stat failed: {e}'
        return p
    if p.nas_size == 0:
        return p
    try:
        with zipfile.ZipFile(rec.nas_zip) as zf:
            names = zf.namelist()
            p.openable = True
            p.entries = len(names)
            p.chans = sum(1 for n in names if n.lower().endswith(('.flac', '.wav')))
            if deep:
                p.deep = True
                bad = zf.testzip()
                if bad:
                    p.crc_bad = bad
                else:
                    p.decode_bad = _decode_check(zf, names)
    except (zipfile.BadZipFile, OSError) as e:
        p.error = f'{type(e).__name__}: {e}'
    return p


def classify(rec: PerfRecord, p: Probe) -> tuple[str, str]:
    """Pure verdict logic over a record + its probe. No disk access — unit-testable."""
    if rec.nas_zip is None:
        if rec.d_zip is not None:
            return 'CROSS_TIER_GAP', 'present on D: only, missing from NAS'
        return 'MISSING', 'no NAS or D: zip'
    if p.nas_size == 0:
        return 'ZERO_BYTES', 'NAS zip is 0 bytes'
    if not p.openable:
        return 'UNOPENABLE', p.error or 'cannot open zip'
    if p.deep and p.crc_bad:
        return 'CRC_FAIL', f'bad CRC member {p.crc_bad}'
    if p.deep and p.decode_bad:
        return 'DECODE_FAIL', f'undecodable member {p.decode_bad}'
    expected = len(rec.d_wavs)
    if expected and p.chans <= max(expected // 4, 1):
        return 'CHANNEL_INCOMPLETE', f'{p.chans} chans vs ~{expected} raw WAVs'
    if p.d_size is not None and p.d_size > 0:
        ratio = p.nas_size / p.d_size
        if ratio < 0.5 or ratio > 2.0:
            return 'PARITY_MISMATCH', f'NAS {p.nas_size} vs D: {p.d_size} bytes'
    if expected and p.chans < expected:
        return 'OK_REVIEW', f'{p.chans}/{expected} chans (silence-drop?)'
    return 'OK', f'{p.entries} entries, {p.chans} chans'


def _gb(n: int | None) -> str:
    return '-' if not n else f'{n / 1024 ** 3:.2f}'


def run(perfs: dict[str, PerfRecord], deep: bool) -> tuple[list[dict], list[str]]:
    """Probe + classify every perf. Returns (json rows, human table lines)."""
    rows: list[dict] = []
    lines: list[str] = []
    header = (f'{"PERF":42} {"NAS_GB":>7} {"D_GB":>7} {"entr":>4} {"chan":>4} '
              f'{"exp":>3}  VERDICT')
    lines.append(header)
    lines.append('-' * len(header))
    for pk in sorted(perfs):
        rec = perfs[pk]
        p = probe_perf(rec, deep)
        verdict, detail = classify(rec, p)
        expected = len(rec.d_wavs)
        rows.append({
            'perf': pk,
            'nas_zip_gb': None if not p.nas_size else round(p.nas_size / 1024 ** 3, 3),
            'd_zip_gb': None if not p.d_size else round(p.d_size / 1024 ** 3, 3),
            'entries': p.entries,
            'chans': p.chans,
            'expected': expected,
            'depth': 'deep' if deep else 'fast',
            'verdict': verdict,
            'detail': detail,
        })
        lines.append(f'{pk:42} {_gb(p.nas_size):>7} {_gb(p.d_size):>7} '
                     f'{p.entries:>4} {p.chans:>4} {expected or "-":>3}  {verdict}: {detail}')
    return rows, lines


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description='Read-only NAS audio deliverable integrity audit.')
    ap.add_argument('--nas-audio', required=True, type=pathlib.Path,
                    help='NAS audio deliverable dir (holds *_MULTITRACK.zip)')
    ap.add_argument('--d-zip', type=pathlib.Path, default=None,
                    help='D: backup zip dir — enables cross-tier parity')
    ap.add_argument('--d-archive', type=pathlib.Path, default=None,
                    help='D: raw-WAV dir — enables channel-expectation + orphan detection')
    ap.add_argument('--out', type=pathlib.Path, default=None, help='write human table here')
    ap.add_argument('--json', type=pathlib.Path, default=None, help='write JSON rows here')
    ap.add_argument('--deep', action='store_true',
                    help='add testzip() CRC + FLAC decode-null (slow)')
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    perfs = build_perfs(args.nas_audio, args.d_zip, args.d_archive)
    rows, lines = run(perfs, args.deep)

    text = '\n'.join(lines)
    print(text)
    if args.out:
        args.out.write_text(text + '\n')
    if args.json:
        args.json.write_text(json.dumps(rows, indent=2) + '\n')

    hard = [r for r in rows if r['verdict'] in HARD_FAIL]
    if hard:
        print(f'\n{len(hard)} hard-fail perf(s): '
              + ', '.join(f'{r["perf"]} [{r["verdict"]}]' for r in hard))
    return 1 if hard else 0


if __name__ == '__main__':
    raise SystemExit(main())
