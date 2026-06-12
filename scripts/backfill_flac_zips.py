"""backfill_flac_zips.py — convert existing WAV `_MULTITRACK.zip` bundles to FLAC.

One-shot, gated migration. For each `*_MULTITRACK.zip` under a target dir:
  1. skip if it already holds `.flac` entries
  2. extract → FLAC-encode each `chan*.wav`
  3. verify decoded-PCM md5(new .flac) == md5(original .wav) for EVERY channel,
     compared at 24-bit (FLAC's ceiling — the source is 32-bit PCM, so FLAC
     keeps the top 24 bits exactly and drops the low 8; see `_decoded_md5`)
  4. only then re-zip ZIP_STORED with `chan*.flac` into a `.tmp`, atomic-rename
     over the original — never delete-before-verify

Run from the CONSOLE (N: visible), not over SSH. Gated by default: --dry-run
previews projected savings; a real run needs --apply; --limit N caps the batch.

Usage:
    uv run python scripts/backfill_flac_zips.py <dir> --dry-run
    uv run python scripts/backfill_flac_zips.py <dir> --apply --limit 5
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import tempfile
import zipfile
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from nofun.audio import _COMPRESS_MISSING, _encode_one_flac


def _decoded_md5(path: pathlib.Path) -> str | None:
    """Decoded-PCM md5 of an audio file at 24-bit (codec-agnostic). None on failure.

    The source WAVs are 32-bit PCM but FLAC's ceiling is 24-bit, so a WAV→FLAC
    round-trip is lossy in the low 8 bits by design. We decode BOTH sides as
    pcm_s24le before hashing, so this verifies the property we actually keep —
    the top 24 bits are preserved bit-exact — and still catches any real encode
    corruption. (ffmpeg's default md5 muxer compares at 16-bit, which would mask
    corruption in bits 17-24.)
    """
    proc = subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(path),
         '-c:a', 'pcm_s24le', '-f', 'md5', '-'],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.startswith('MD5='):
            return line[4:].strip()
    return None


def _zip_has_flac(zip_path: pathlib.Path) -> bool:
    with zipfile.ZipFile(zip_path) as z:
        return any(n.lower().endswith('.flac') for n in z.namelist())


def _write_stored_zip(out_path: pathlib.Path,
                      entries: list[tuple[str, bytes]]) -> None:
    """Write FLAC bytes into out_path with ZIP_STORED (no second compression)."""
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_STORED,
                         allowZip64=True) as zf:
        for arcname, data in entries:
            zf.writestr(arcname, data)


def _convert_one(zip_path: pathlib.Path, apply: bool) -> tuple[bool, int, int]:
    """Convert one WAV-zip to FLAC. Returns (changed, old_bytes, new_bytes)."""
    old_bytes = zip_path.stat().st_size
    try:
        return _convert_one_inner(zip_path, apply, old_bytes)
    except (zipfile.BadZipFile, EOFError, zlib.error, OSError) as e:
        print(f"  BAD   {zip_path.name}  (unreadable: {e}) — left untouched")
        return False, old_bytes, old_bytes


def _convert_one_inner(zip_path: pathlib.Path, apply: bool,
                       old_bytes: int) -> tuple[bool, int, int]:
    if _zip_has_flac(zip_path):
        print(f"  SKIP  {zip_path.name}  (already FLAC)")
        return False, old_bytes, old_bytes

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp_path)
        # Engine WAVs are '<perf>_chanN.wav' — match anywhere in the name
        wavs = sorted(tmp_path.glob('*chan*.wav'))
        if not wavs:
            print(f"  SKIP  {zip_path.name}  (no chan*.wav inside)")
            return False, old_bytes, old_bytes
        extras = [p for p in tmp_path.rglob('*') if p.is_file() and p not in wavs]
        if extras:
            print(f"  SKIP  {zip_path.name}  ({len(extras)} non-chan member(s) "
                  f"e.g. {extras[0].name} — won't rewrite a mixed zip)")
            return False, old_bytes, old_bytes

        entries: list[tuple[str, bytes]] = []
        for wav in wavs:
            result = _encode_one_flac(wav)
            if result is _COMPRESS_MISSING:
                print(f"  FAIL  {zip_path.name}  ({wav.name} failed to encode)")
                return False, old_bytes, old_bytes
            arcname, flac_bytes, _size, _crc = result

            flac_tmp = tmp_path / arcname
            flac_tmp.write_bytes(flac_bytes)
            if _decoded_md5(flac_tmp) != _decoded_md5(wav):
                print(f"  FAIL  {zip_path.name}  ({wav.name} md5 mismatch — "
                      f"original untouched)")
                return False, old_bytes, old_bytes
            entries.append((arcname, flac_bytes))

        new_bytes = sum(len(d) for _n, d in entries)
        # zip overhead is small; report the FLAC payload as the projection.
        if not apply:
            print(f"  DRY   {zip_path.name}  {old_bytes/1e6:.1f}MB -> "
                  f"~{new_bytes/1e6:.1f}MB")
            return True, old_bytes, new_bytes

        staging = zip_path.with_suffix('.zip.tmp')
        _write_stored_zip(staging, entries)
        actual_new = staging.stat().st_size
        staging.replace(zip_path)        # atomic — old zip only now gone
        print(f"  OK    {zip_path.name}  {old_bytes/1e6:.1f}MB -> "
              f"{actual_new/1e6:.1f}MB")
        return True, old_bytes, actual_new


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('target', help='directory to scan for *_MULTITRACK.zip')
    ap.add_argument('--apply', action='store_true',
                    help='actually rewrite zips (default: dry run)')
    ap.add_argument('--dry-run', action='store_true',
                    help='preview projected savings (default)')
    ap.add_argument('--limit', type=int, default=0,
                    help='cap the number of zips converted this run (0 = all)')
    args = ap.parse_args()

    apply = args.apply and not args.dry_run
    target = pathlib.Path(args.target)
    if not target.is_dir():
        sys.exit(f"target not a directory: {target}")

    zips = sorted(target.rglob('*_MULTITRACK.zip'))
    print(f"{len(zips)} _MULTITRACK.zip under {target}"
          f"{'  (DRY RUN)' if not apply else ''}")

    converted = total_old = total_new = 0
    for zip_path in zips:
        if args.limit and converted >= args.limit:
            print(f"  -- limit {args.limit} reached, stopping --")
            break
        changed, old_b, new_b = _convert_one(zip_path, apply)
        if changed:
            converted += 1
            total_old += old_b
            total_new += new_b

    saved = total_old - total_new
    print(f"\n{converted} converted{'' if apply else ' (projected)'}; "
          f"{total_old/1e9:.2f}GB -> {total_new/1e9:.2f}GB "
          f"(~{saved/1e9:.2f}GB saved)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
