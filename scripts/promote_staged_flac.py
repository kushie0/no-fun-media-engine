"""promote_staged_flac.py — atomic gated-swap: rename .zip.FLAC → .zip
and delete the original WAV zip for one month's staged conversions.

After the user explicitly approves a month, this script:
  1. Verifies every staged .zip.FLAC. Default = structural (all-FLAC, ZIP_STORED,
     smallest channel decodes). With --paranoid it re-decodes EVERY channel and
     compares 24-bit PCM md5 against the original WAV zip (still on disk),
     closing the single-channel gap before the WAV is deleted.
  2. Confirms a D: backup of the original WAV exists (D:\\audio\\<name>_MULTITRACK.zip,
     the 180-day raw-backup tier) so the delete stays recoverable for ≤180 days.
     Refuses to delete any zip lacking that backup unless --allow-no-backup.
  3. Dry-run by default — prints exactly what it would do.
  4. --apply: deletes the WAV original and renames .zip.FLAC → .zip.

Never runs without --apply. The 32→24 bit commit becomes truly irreversible only
once BOTH the NAS WAV and its D: backup are gone.

Usage:
  uv run python scripts/promote_staged_flac.py 26-03-                      # dry-run
  uv run python scripts/promote_staged_flac.py 26-03- --paranoid           # dry-run, full md5
  uv run python scripts/promote_staged_flac.py 26-03- --paranoid --apply   # THE REAL THING
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / 'scripts'))
import backfill_flac_zips as bf  # noqa: E402

NAS_AUDIO = pathlib.Path(os.environ.get(
    'NAS_AUDIO_DIR', r'\\192.168.0.232\nofun-archive\audio'))
D_AUDIO = pathlib.Path(os.environ.get('D_AUDIO_DIR', r'D:\audio'))

_PREFIX_RE = re.compile(r'^\d{2}-\d{2}-$')


def _discover(prefix: str) -> list[pathlib.Path]:
    """Find all staged .zip.FLAC files for a month on the NAS."""
    return sorted(NAS_AUDIO.glob(f'{prefix}*_MULTITRACK.zip.FLAC'))


def _wav_original(flac_path: pathlib.Path) -> pathlib.Path:
    """The original WAV zip that this .FLAC was staged from."""
    return flac_path.with_suffix('')  # strip .FLAC → .zip


def _d_backup(flac_path: pathlib.Path) -> pathlib.Path:
    """The D: 180-day raw-backup copy of the original WAV zip for this show."""
    return D_AUDIO / _wav_original(flac_path).name


def promote(prefix: str, apply: bool = False, paranoid: bool = False,
            allow_no_backup: bool = False) -> int:
    if not _PREFIX_RE.match(prefix):
        print(f'refusing unscoped prefix {prefix!r} — must match YY-MM-')
        return 2

    staged = _discover(prefix)
    if not staged:
        print(f'no {prefix}*_MULTITRACK.zip.FLAC found on NAS')
        return 0

    print(f'NAS_AUDIO = {NAS_AUDIO}')
    print(f'D_AUDIO   = {D_AUDIO}   (180-day raw-backup tier)')
    print(f'verify    = {"PARANOID (every channel md5 vs WAV)" if paranoid else "standard (structural + smallest channel)"}')
    print(f'\n{len(staged)} staged {prefix}*_MULTITRACK.zip.FLAC found\n')

    # Phase 1: verify every staged FLAC + check the D: backup of each WAV.
    results = []
    all_ok = True
    nas_wav_gb = nas_flac_gb = 0.0

    print(f'{"zip":48} {"verify":24} {"WAV_GB":>7} {"FLAC_GB":>7} {"D:bkup":>7}')
    print('-' * 98)

    for flac in staged:
        wav = _wav_original(flac)
        v_result = (bf.verify_flac_zip_paranoid(flac, wav) if paranoid
                    else bf.verify_flac_zip(flac))
        ok = v_result.startswith('OK')

        wav_sz = wav.stat().st_size / 1e9 if wav.exists() else 0
        flac_sz = flac.stat().st_size / 1e9
        nas_wav_gb += wav_sz
        nas_flac_gb += flac_sz

        d_ok = _d_backup(flac).exists()
        results.append({'flac': flac, 'wav': wav, 'ok': ok, 'd_ok': d_ok})

        print(f'{flac.name:48.48} {v_result:24.24} {wav_sz:7.2f} {flac_sz:7.2f} '
              f'{("yes" if d_ok else "NO"):>7}')
        if not ok:
            all_ok = False

    if nas_wav_gb > 0:
        pct = (1 - nas_flac_gb / nas_wav_gb) * 100
        print(f'\nWAV to delete: {nas_wav_gb:.1f} GB -> FLAC: {nas_flac_gb:.1f} GB ({pct:.0f}% shrink)')
    else:
        print('\nWAV original(s) already gone -- nothing to reclaim.')

    if not all_ok:
        n_failed = sum(1 for r in results if not r['ok'])
        print(f'\nVERIFICATION FAILED -- cannot promote until all zips verify OK '
              f'({n_failed} failed).')
        return 1

    no_bk = [r for r in results if not r['d_ok']]
    if no_bk:
        print(f'\n{len(no_bk)} zip(s) have NO D: backup -- deleting their WAV would be '
              f'truly irreversible:')
        for r in no_bk:
            print(f'    {r["wav"].name}')
        if allow_no_backup:
            print('   --allow-no-backup set: these WILL be deleted.')
        elif apply:
            print('   refusing (pass --allow-no-backup to override). Backed-up zips '
                  'below would still proceed.')

    print(f'\nAll {len(staged)} zips verified OK'
          f'{" (paranoid: every channel md5-matched the WAV)" if paranoid else ""}.')
    reclaim = nas_wav_gb

    if not apply:
        print('\nDRY RUN -- no changes made. Run with --apply to execute.')
        print(f'Would reclaim ~{reclaim:.1f} GB'
              f'{f"; {len(no_bk)} blocked on missing D: backup" if no_bk else ""}.')
        return 0

    # Phase 2: apply -- per zip: delete WAV, rename .FLAC -> .zip.
    print('\n' + '=' * 60)
    print(f'APPLYING promotion for {prefix} -- irreversible')
    print('=' * 60)

    ok_count = fail_count = skip_count = 0
    reclaimed = 0.0
    for r in results:
        flac, wav, d_ok = r['flac'], r['wav'], r['d_ok']
        if not d_ok and not allow_no_backup:
            print(f'  SKIP {wav.name}: no D: backup (kept)')
            skip_count += 1
            continue
        try:
            sz = wav.stat().st_size if wav.exists() else 0
            if wav.exists():
                wav.unlink()
            flac.rename(wav)
            ok_count += 1
            reclaimed += sz / 1e9
            print(f'  OK   {wav.name}  (WAV deleted, FLAC {wav.stat().st_size/1e6:.0f} MB)')
        except Exception as e:
            print(f'  FAIL {flac.name}: {e}')
            fail_count += 1

    print(f'\n{ok_count} promoted, {fail_count} failed, {skip_count} skipped (no D: backup)')
    if ok_count:
        print(f'Reclaimed ~{reclaimed:.1f} GB on NAS (WAV still recoverable from D: '
              f'for ≤180 days where backed up).')
        print('Next: wait for the D: mirror, then verify the promoted .zip appeared '
              'there before any D: cleanup.')
    return 0 if fail_count == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('prefix', help='Month prefix (e.g. 26-03-)')
    ap.add_argument('--apply', action='store_true',
                    help='Delete WAVs and rename .FLAC -> .zip (dry-run without this)')
    ap.add_argument('--paranoid', action='store_true',
                    help='Re-decode every channel and md5-compare to the WAV original')
    ap.add_argument('--allow-no-backup', action='store_true',
                    help='Permit deleting WAVs that have no D: backup (truly irreversible)')
    a = ap.parse_args()
    return promote(a.prefix, apply=a.apply, paranoid=a.paranoid,
                   allow_no_backup=a.allow_no_backup)


if __name__ == '__main__':
    raise SystemExit(main())
