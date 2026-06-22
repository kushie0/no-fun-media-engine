"""nofun/storage_config.py — the single registry of where the engine stores files.

One frozen object describes a deployment's storage *layout*: the fixed roots, the
SharePoint tenant path, the four media subdir names, and the D: raw-backup tier. Both
inventory and RENAME derive their location lists from it, so a new storage tier is added
in one place and never silently missed (the failure that left a band half-renamed when
the D: backup tier was referenced only as inline literals).

`media_root` itself stays on the Pipeline because it is runtime-mutable (NAS↔D: fallback);
this config owns the layout *names*, and `all_storage_roots(media_root)` composes the full
set against whatever the live media_root currently is.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib

__all__ = ['StorageConfig']


@dataclasses.dataclass(frozen=True)
class StorageConfig:
    """Static storage layout for one deployment. Build via :meth:`from_env`."""

    mount_c: pathlib.Path
    mount_d: pathlib.Path
    search_dir: pathlib.Path                 # source watch dir (VenueLighting)
    clips_dest: pathlib.Path                 # C: SSD — never follows media_root
    sharepoint_dest: pathlib.Path | None     # OneDrive Multitracks (None if absent)
    # Subdir names under media_root (were hardcoded in _set_media_root). The D:
    # raw-backup tier reuses video_archive_subdir / audio_subdir under mount_d.
    videos_subdir: str = 'videos'
    audio_subdir: str = 'audio'
    video_archive_subdir: str = 'video_archive'
    audio_archive_subdir: str = 'audio_archive'

    # --- D: raw-backup tier (always on mount_d, separate from media_root) ---
    @property
    def d_video_backup(self) -> pathlib.Path:
        return self.mount_d / self.video_archive_subdir

    @property
    def d_audio_backup(self) -> pathlib.Path:
        return self.mount_d / self.audio_subdir

    def media_dests(self, media_root: pathlib.Path) -> dict[str, pathlib.Path]:
        """The four media-root-derived dests for the given (live) media_root."""
        return {
            'vids_dest':     media_root / self.videos_subdir,
            'audio_dest':    media_root / self.audio_subdir,
            'video_archive': media_root / self.video_archive_subdir,
            'audio_archive': media_root / self.audio_archive_subdir,
        }

    def all_storage_roots(self, media_root: pathlib.Path) -> list[pathlib.Path]:
        """Every location that can hold band-named files, for RENAME / scan.

        Includes the D: backup tier — the location the old registry missed.
        SharePoint is included; callers that handle the cloud separately (RENAME)
        filter it out, as they did before.
        """
        d = self.media_dests(media_root)
        roots = [
            self.search_dir,
            self.clips_dest,
            d['vids_dest'], d['audio_dest'], d['video_archive'], d['audio_archive'],
            self.d_video_backup, self.d_audio_backup,
        ]
        if self.sharepoint_dest:
            roots.append(self.sharepoint_dest)
        return roots

    @classmethod
    def from_env(cls, mount_c: pathlib.Path, mount_d: pathlib.Path,
                 search_dir: pathlib.Path, clips_dest: pathlib.Path) -> 'StorageConfig':
        """Build from env, defaulting to today's exact paths when unset.

        Roots (mounts, search_dir, clips_dest) are resolved by the caller via the
        existing paths.py detectors and passed in. This adds the layout overrides:
        ``VIDEOS_SUBDIR``, ``AUDIO_SUBDIR``, ``VIDEO_ARCHIVE_SUBDIR``,
        ``AUDIO_ARCHIVE_SUBDIR``, and ``SHAREPOINT_DEST``.
        """
        sp_env = os.environ.get('SHAREPOINT_DEST')
        if sp_env:
            sp = pathlib.Path(sp_env)
        else:
            user = os.environ.get('USERNAME') or os.environ.get('USER') or 'nofun'
            sp = mount_c / 'Users' / user / 'OneDrive - No Fun Troy LLC' / 'Multitracks'
        return cls(
            mount_c=mount_c,
            mount_d=mount_d,
            search_dir=search_dir,
            clips_dest=clips_dest,
            sharepoint_dest=sp if sp.is_dir() else None,
            videos_subdir=os.environ.get('VIDEOS_SUBDIR', 'videos'),
            audio_subdir=os.environ.get('AUDIO_SUBDIR', 'audio'),
            video_archive_subdir=os.environ.get('VIDEO_ARCHIVE_SUBDIR', 'video_archive'),
            audio_archive_subdir=os.environ.get('AUDIO_ARCHIVE_SUBDIR', 'audio_archive'),
        )

    def validate(self, logger: logging.Logger) -> None:
        """Log the resolved layout once and warn on a missing source dir.

        Warn-and-degrade by design: clips_dest and the media dests are created by
        the Pipeline at startup, and a missing source dir or SharePoint folder must
        not block boot (the watchdog picks them up when they appear; the NAS has a
        runtime fallback). So this never raises — it surfaces the layout for a
        deployment to eyeball and flags the one root that silently breaks ingestion.
        """
        logger.info(
            "Storage layout  search=%s  clips=%s  sharepoint=%s  "
            "media subdirs=%s/%s/%s/%s  D-backup=%s,%s",
            self.search_dir, self.clips_dest,
            self.sharepoint_dest if self.sharepoint_dest else '(none)',
            self.videos_subdir, self.audio_subdir,
            self.video_archive_subdir, self.audio_archive_subdir,
            self.d_video_backup, self.d_audio_backup,
        )
        if not self.search_dir.exists():
            logger.warning(
                "Storage: source dir %s does not exist yet — no recordings will be "
                "found until it appears", self.search_dir,
            )
        if self.sharepoint_dest is None:
            logger.warning("Storage: no SharePoint folder — cloud sync disabled")
