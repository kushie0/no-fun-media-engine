"""nofun/paths.py — Platform detection and drive-mount resolution."""

__all__ = [
    'detect_platform',
    'detect_mounts',
    'detect_clips_root',
    'is_windows',
    'is_windows_native',
    'is_darwin',
    'NULL_DEV',
]

import os
import pathlib
import platform
import sys


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
