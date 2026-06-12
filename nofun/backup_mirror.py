"""Copy-only file mirroring + age-based selection for the local D: backup tier.

The engine writes media to the NAS primary (``media_root``). Two callers use this:

  - the **raw backup** down-mirror copies raw originals (``.mov`` + ``_MULTITRACK.zip``)
    N:→D:, gated to a rolling retention window, so a NAS outage still leaves the
    *inputs* needed to regenerate everything (deliverables are deterministic, so
    they are NOT backed up — that would be redundant storage of recoverable data);
  - the **failback reconcile** copies deliverables D:→N: after a NAS outage.

Copy-only by construction: nothing at the destination is ever deleted by the mirror,
so files that exist only on the backup tier survive. Expiry (``find_expired`` +
the engine's unlink) is the *only* thing that removes D: files, and it shares the
retention boundary with the mirror's ``include`` gate so the two never thrash.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

__all__ = ['DELIVERABLE_EXTS', 'RAW_BACKUP_EXTS', 'mirror_files', 'find_expired']

DELIVERABLE_EXTS = ('.mp4', '.mp3')
RAW_BACKUP_EXTS = ('.mov', '.zip')


def mirror_files(
    pairs: list[tuple[Path, Path]],
    exts: tuple[str, ...] = DELIVERABLE_EXTS,
    include: Callable[[Path], bool] | None = None,
) -> tuple[int, int]:
    """Copy files (matched by extension) from each ``src`` to ``dst``.

    Walks each ``src`` recursively. A file is copied when it is missing at the
    destination, or present with a *different* size (a re-master replaces the
    stale backup). Files already present with a matching size are skipped.
    Nothing is ever deleted, so destination-only files survive.

    When ``include`` is given, a file is considered only if ``include(f)`` is
    True — used to gate the raw backup to its retention window so an expired file
    is not re-copied on the next run. ``None`` copies every matching file.

    Returns ``(copied, skipped)``.
    """
    want = tuple(e.lower() for e in exts)
    copied = skipped = 0
    for src, dst in pairs:
        if not src.is_dir():
            continue
        for f in src.rglob('*'):
            if f.suffix.lower() not in want or not f.is_file():
                continue
            if include is not None and not include(f):
                continue
            try:
                target = dst / f.relative_to(src)
                if target.exists() and target.stat().st_size == f.stat().st_size:
                    skipped += 1
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                copied += 1
            except OSError:
                # A NAS read or local write hiccup on one file must not abort the
                # whole mirror — skip it; the next scheduled run retries.
                continue
    return copied, skipped


def find_expired(
    pairs: list[tuple[Path, tuple[str, ...]]],
    is_old: Callable[[Path], bool],
) -> list[Path]:
    """Files under each ``dir`` matching its ``exts`` for which ``is_old(path)`` is True.

    Pure selection — performs no deletion. Each pair is ``(dir, exts)`` so the
    caller can scope different extensions per directory (``.mov`` in
    ``video_archive``, ``.zip`` in ``audio``). Missing dirs are skipped.
    """
    stale: list[Path] = []
    for d, exts in pairs:
        if not d.is_dir():
            continue
        want = tuple(e.lower() for e in exts)
        for f in d.rglob('*'):
            if f.suffix.lower() in want and f.is_file() and is_old(f):
                stale.append(f)
    return stale
