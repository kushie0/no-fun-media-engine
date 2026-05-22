"""strip_cloud_dates.py — remove leading date prefixes from SharePoint cloud files.

For each folder (or a single folder passed as argv[1]):
  - DELETE dated files where an undated clean copy already exists
  - RENAME dated-only files (reels, FULLSETs) to strip the prefix
  - Regenerate _nofun_info.txt, preserving the original expiry date

Usage:
    uv run python scripts/strip_cloud_dates.py                  # all folders
    uv run python scripts/strip_cloud_dates.py <folder_path>    # one folder
    uv run python scripts/strip_cloud_dates.py --dry-run        # preview only
"""

import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from nofun.cleanup import _read_expiry_date, write_sharepoint_info
from nofun.paths import detect_mounts

_DATE_RE   = re.compile(r'^(?:\d{2}-\d{1,2}-\d{1,2}|\d{8})_')
_SKIP      = {'_nofun_info.txt'}
_MEDIA_EXT = {'.mp4', '.mp3', '.wav', '.zip'}


def _strip_folder(folder: pathlib.Path, dry_run: bool) -> int:
    dated = [
        f for f in folder.iterdir()
        if f.is_file() and _DATE_RE.match(f.name) and f.name not in _SKIP
    ]
    if not dated:
        return 0

    expiry = _read_expiry_date(folder)
    if expiry is None:
        print(f"  SKIP {folder.name}: no expiry date in info file")
        return 0

    ops = 0
    for f in sorted(dated):
        stripped = _DATE_RE.sub('', f.name, count=1)
        twin = folder / stripped
        if twin.exists():
            print(f"  DELETE  {f.name}")
            if not dry_run:
                f.unlink()
        else:
            print(f"  RENAME  {f.name}  ->  {stripped}")
            if not dry_run:
                f.rename(folder / stripped)
        ops += 1

    if not dry_run:
        media_files = sorted(
            f for f in folder.iterdir()
            if f.is_file() and f.suffix in _MEDIA_EXT and f.name not in _SKIP
        )
        write_sharepoint_info(folder, media_files, expiry, new_files=[])
        print(f"  INFO    regenerated ({len(media_files)} files, expiry {expiry})")

    return ops


def main() -> None:
    args = sys.argv[1:]
    dry_run = '--dry-run' in args
    args = [a for a in args if a != '--dry-run']

    mount_c, _ = detect_mounts()
    username = os.environ.get('USERNAME') or os.environ.get('USER') or 'nofun'
    sp_dest = mount_c / 'Users' / username / 'OneDrive - No Fun Troy LLC' / 'Multitracks'
    if not sp_dest.is_dir():
        sys.exit(f"sharepoint_dest not found: {sp_dest}")

    if args:
        folders = [pathlib.Path(args[0])]
    else:
        folders = sorted(
            d for d in sp_dest.iterdir()
            if d.is_dir() and d.name != 'archived'
        )

    total = 0
    for folder in folders:
        print(folder.name)
        total += _strip_folder(folder, dry_run)

    suffix = " (dry run)" if dry_run else ""
    print(f"\n{total} file(s) processed{suffix}")


if __name__ == '__main__':
    main()
