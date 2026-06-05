"""Back up finished deliverables from the NAS primary down to a local tier.

The engine writes videos/audio to the NAS (``media_root``). This mirrors only
the *finished cuts* — ``.mp4`` and ``.mp3`` — down to the local D: drive so the
deliverables survive a NAS outage. The heavy raw/intermediate tiers
(``.mov`` / ``.wav`` / ``.zip``) are deliberately excluded by extension: they
stay NAS-only.

Copy-only by construction: nothing at the destination is ever deleted, so files
that exist only on the backup tier are left untouched.
"""
from __future__ import annotations

import shutil
from pathlib import Path

__all__ = ['DELIVERABLE_EXTS', 'mirror_deliverables']

DELIVERABLE_EXTS = ('.mp4', '.mp3')


def mirror_deliverables(
    pairs: list[tuple[Path, Path]],
    exts: tuple[str, ...] = DELIVERABLE_EXTS,
) -> tuple[int, int]:
    """Copy deliverable files (matched by extension) from each ``src`` to ``dst``.

    Walks each ``src`` recursively. A file is copied when it is missing at the
    destination, or present with a *different* size (a re-master replaces the
    stale backup). Files already present with a matching size are skipped.
    Nothing is ever deleted, so destination-only files survive.

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
