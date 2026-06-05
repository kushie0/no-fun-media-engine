"""nofun/paths.py — Platform detection and drive-mount resolution."""

__all__ = [
    'detect_platform',
    'detect_mounts',
    'detect_clips_root',
    'detect_media_root',
    'nas_reachable',
    'is_windows',
    'is_windows_native',
    'is_darwin',
    'NULL_DEV',
]

import os
import pathlib
import platform
import sys
import threading


def detect_platform() -> str:
    """Return 'darwin' | 'wsl' | 'windows' | 'gitbash' | 'linux'."""
    if sys.platform == 'darwin':
        return 'darwin'
    release = platform.uname().release.lower()
    if 'microsoft' in release:
        return 'wsl'
    if sys.platform == 'win32':
        # MSYSTEM is set by Git Bash; absence means native Windows Python
        return 'gitbash' if 'MSYSTEM' in os.environ else 'windows'
    return 'linux'


def detect_mounts() -> tuple[pathlib.Path, pathlib.Path]:
    """Return (MOUNT_C, MOUNT_D).  Falls back to '.' for local dev."""
    # Honour explicit env override (used in tests and trial runs)
    mount_d_env = os.environ.get('MOUNT_D')
    if mount_d_env:
        mount_d = pathlib.Path(mount_d_env)
        mount_c_env = os.environ.get('MOUNT_C')
        if mount_c_env:
            return pathlib.Path(mount_c_env), mount_d
        if sys.platform == 'win32':
            return pathlib.Path('C:/'), mount_d
        if pathlib.Path('/mnt/c').is_dir():
            return pathlib.Path('/mnt/c'), mount_d
        if pathlib.Path('/c').is_dir():
            return pathlib.Path('/c'), mount_d
        return pathlib.Path('.'), mount_d

    # Native Windows (PowerShell / cmd)
    if sys.platform == 'win32' and 'MSYSTEM' not in os.environ:
        return pathlib.Path('C:/'), pathlib.Path('D:/')
    # WSL
    if pathlib.Path('/mnt/c').is_dir():
        return pathlib.Path('/mnt/c'), pathlib.Path('/mnt/d')
    # Git Bash
    if pathlib.Path('/c').is_dir():
        return pathlib.Path('/c'), pathlib.Path('/d')
    return pathlib.Path('.'), pathlib.Path('.')


def detect_clips_root(mount_d: pathlib.Path) -> pathlib.Path:
    """Return the directory where encoded clip outputs live.

    Honours the CLIPS_ROOT env var; falls back to ``mount_d / 'clips'`` so
    historical setups (clips on D:) keep working without configuration.

    Set CLIPS_ROOT to put clips on a faster disk (e.g. C:\\clips on an SSD
    when D: is an HDD shared with streaming reads).
    """
    env = os.environ.get('CLIPS_ROOT')
    return pathlib.Path(env) if env else (mount_d / 'clips')


def detect_media_root(mount_d: pathlib.Path) -> pathlib.Path:
    """Return the root for primary media output (videos, audio ZIPs, video_archive).

    Honours NAS_ROOT (e.g. a UNC path \\\\host\\share). If NAS_ROOT is set and
    reachable, output goes there; otherwise falls back to mount_d so a NAS outage
    — or a non-prod machine where NAS_ROOT is unset — cleanly uses the local drive.
    """
    env = os.environ.get('NAS_ROOT')
    if env:
        root = pathlib.Path(env)
        try:
            # is_dir() can *raise* (not just return False) when a UNC share is
            # unreachable — guard so a NAS outage falls back instead of crashing.
            if root.is_dir():
                return root
        except OSError:
            pass
    return mount_d


def nas_reachable(root: pathlib.Path, timeout: float = 3.0) -> bool:
    """True if ``root`` is a reachable directory, within ``timeout`` seconds.

    A disconnected SMB/UNC share can make ``is_dir()`` *block* for seconds before
    raising, which would stall the engine's main loop. Run the probe in a daemon
    thread and join with a timeout: if the thread is still alive (still blocking)
    when the join returns, treat the share as unreachable.
    """
    result = {'ok': False}

    def _check() -> None:
        try:
            result['ok'] = root.is_dir()
        except OSError:
            result['ok'] = False

    t = threading.Thread(target=_check, daemon=True)
    t.start()
    t.join(timeout)
    return result['ok']   # still alive at timeout → ok stays False


def is_windows() -> bool:
    """True on any Windows environment (native, Git Bash, WSL-not-used here)."""
    return sys.platform == 'win32' or 'MSYSTEM' in os.environ


def is_windows_native() -> bool:
    """True only on native Windows Python (not Git Bash)."""
    return sys.platform == 'win32' and 'MSYSTEM' not in os.environ


def is_darwin() -> bool:
    """True on macOS."""
    return sys.platform == 'darwin'


# NUL on Git Bash/Windows, /dev/null everywhere else
NULL_DEV = 'NUL' if (
    sys.platform == 'win32' or 'MSYSTEM' in os.environ) else '/dev/null'
