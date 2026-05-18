"""One-shot: drop encoding_db.json clip records whose path no longer exists.

Run after the empty-folder cleanup in Phase 3. Prints a report and asks for
confirmation before writing back to encoding_db.json.

TODO: delete this script once Phase 4 (encoding_db schema 2 / clips_summary)
migration has run on the production host — it targets the old per-clip schema.
"""
from __future__ import annotations

import pathlib
import sys

from nofun.encoding_db import EncodingDB


def main() -> int:
    db_path = pathlib.Path(__file__).parent.parent / 'encoding_db.json'
    db = EncodingDB(db_path)

    pruned = 0
    bands_touched: list[tuple[str, str]] = []
    for date, bands in db._data.get('performances', {}).items():
        for band, perf in bands.items():
            clips = perf.get('clips')
            if not isinstance(clips, list):
                continue
            survivors = [r for r in clips if pathlib.Path(r.get('path', '')).exists()]
            removed = len(clips) - len(survivors)
            if removed:
                perf['clips'] = survivors
                pruned += removed
                bands_touched.append((date, band))

    print(f"would prune {pruned} stale clip record(s) across "
          f"{len(bands_touched)} (date, band) entries")
    if not pruned:
        return 0
    if input("write changes to encoding_db.json? [y/N] ").strip().lower() != 'y':
        return 1

    db._rebuild_index()
    db.save()
    print(f"saved encoding_db.json ({pruned} records pruned)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
