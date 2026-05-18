"""nofun/reel.py — Instagram Reel generator (9:16 vertical video).

Stacks the four quadrant MP4s vertically at native resolution (UL / UR / LL / LR),
applies a continuous looping scroll using ffmpeg's crop filter with a time-varying
y expression so that as the top quad exits the bottom one enters seamlessly,
then crops to a 9:16 window. Mixes in the FULLSET WAV as audio.

Scroll speed: base is 10× slower than the legacy constant rate. Speed oscillates
sinusoidally between 0.5× and 1.5× base on a 30-second cycle, giving a gentle
organic feel without overwhelming ffmpeg's expression evaluator.
All in a single ffmpeg pass with no extra dependencies.
"""

from __future__ import annotations

import logging
import math
import pathlib
import subprocess
import tempfile
import time
from collections.abc import Callable

from nofun.media_io import probe_stream, probe_format, run_ffmpeg, fmt_size


__all__ = ['generate_reel']

_SECS_PER_QUAD   = 20.0   # base: one quad height per 120 s (10× slower than legacy 12 s)
_SCROLL_PERIOD   =  30.0   # seconds per sinusoidal speed oscillation cycle
_SCROLL_VARIANCE =   0.5   # amplitude as fraction of base speed (0.5 → ±50%, so 0.5×–1.5×)
_AUDIO_DELAY_S   =   0.250 # seconds to delay audio relative to video (positive = audio later)


def _variable_scroll_y(strip_h: int, speed_px_s: float, duration: float) -> str:  # noqa: ARG001
    """Build an ffmpeg crop-filter *y* expression for variable-speed looping scroll.

    Uses a sinusoidal velocity profile so the expression stays short enough for
    ffmpeg's expression evaluator (libavutil/eval.c has an internal AST node limit
    that the previous burst-schedule approach — ~140 additive terms — exceeded,
    causing "Missing ')'" errors regardless of expression length or operator choice).

    Maths
    -----
    velocity(t) = base + B·sin(ω·t)

    Integrating: y(t) = base·t + (B/ω)·(1 − cos(ω·t))

    where B = _SCROLL_VARIANCE · base  (so speed varies from 0.5× to 1.5× base)
    and   ω = 2π / _SCROLL_PERIOD

    At t=0, y=0.  The minimum velocity is base − B = 0.5·base > 0, so y is strictly
    increasing (no backward motion).  The average velocity equals base, so the
    overall scroll rate is unchanged.

    The resulting expression is ~55 characters — a single multiplication, a cosine,
    and a mod() call — comfortably within any ffmpeg Eval limit.

    Operator note: use mod(x\\,y) not x%y.  ffmpeg's crop filter expression evaluator
    rejects '%' with "Invalid chars" on the Windows build.  The comma inside mod() must
    be escaped as \\, even in a script file — ffmpeg's filter_complex parser treats an
    unescaped comma as a filter-chain separator regardless of how the graph is supplied.

    Wrap-around: the y expression produces values in [0, strip_h).  The caller
    passes a *doubled* strip (2×strip_h), so the crop window (out_h < strip_h)
    always fits; the second copy of the content makes the loop seamless.
    """
    omega        = 2.0 * math.pi / _SCROLL_PERIOD
    B            = _SCROLL_VARIANCE * speed_px_s
    B_over_omega = B / omega
    inner = (
        f"{speed_px_s:.6f}*t"
        f"+{B_over_omega:.6f}*(1-cos({omega:.6f}*t))"
    )
    # \, escapes the comma so ffmpeg's filter_complex parser doesn't treat it
    # as a filter-chain separator (same rule applies in script files as inline).
    return f"mod({inner}\\,{strip_h})"


def generate_reel(
    quad_files: dict[str, pathlib.Path],
    fullset_wav: pathlib.Path,
    out_path: pathlib.Path,
    logger: logging.Logger,
    enc: dict,  # noqa: ARG001  kept for API compat; reel always uses libx264
    trial_run: int = 0,
    seek: float = 0.0,
    progress_cb: Callable[[str, str, str, str], None] | None = None,
    proc_cb: Callable[[subprocess.Popen], None] | None = None,
    script_runner=None,   # ScriptRunner | None — uses scripts/generate_reel.py when set
) -> bool:
    """Render a quad_w×1920 vertical reel from four quadrant MP4s + FULLSET WAV.

    Parameters
    ----------
    quad_files  : {'UL': Path, 'UR': Path, 'LL': Path, 'LR': Path}
    fullset_wav : path to the *_FULLSET.wav audio file
    out_path    : desired final output path (e.g. reels_dest / '{base}_reel.mp4')
    logger      : pipeline logger
    enc         : encoder config dict from build_encoder_config()
    trial_run   : if >0, encode only this many seconds (fast test)

    Returns True on success, False on failure.
    """
    _order = ('UL', 'UR', 'LL', 'LR')
    missing = [k for k in _order if not (quad_files.get(k) and quad_files[k].exists())]
    if missing:
        logger.error(f"REEL  missing quad file(s): {', '.join(missing)}")
        return False
    quads = [quad_files[k] for k in _order]

    if not fullset_wav.exists():
        logger.error(f"REEL  FULLSET WAV not found: {fullset_wav.name}")
        return False

    # ------------------------------------------------------------------ probe
    ref = quads[0]
    w_str = probe_stream(ref, 'width')
    h_str = probe_stream(ref, 'height')
    dur_str = probe_format(ref, 'duration')

    try:
        src_w, src_h = int(w_str), int(h_str)
    except (ValueError, TypeError):
        logger.error(f"REEL  could not probe dimensions of {ref.name}")
        return False

    try:
        duration = float(dur_str)
    except (ValueError, TypeError):
        duration = 0.0

    # Native resolution — no scaling. Output is 9:16 vertical (Instagram Reels).
    # out_h = src_w × 16/9, clamped to the strip height and rounded down to even.
    strip_h = src_h * 4
    out_h   = min(strip_h, int(src_w * 16 / 9) // 2 * 2)
    speed   = src_h / _SECS_PER_QUAD   # base scroll speed in px/s

    base = out_path.stem
    logger.info(
        f"REEL  {base}  "
        f"({src_w}×{src_h} native, strip {strip_h}px → {src_w}×{out_h} output, "
        f"base scroll {speed:.2f}px/s ({_SECS_PER_QUAD:.0f}s/quad), "
        f"±{_SCROLL_VARIANCE*100:.0f}% sine on {_SCROLL_PERIOD:.0f}s cycle"
        + (f", dur {int(duration//60)}:{int(duration%60):02d}" if duration else "")
        + ")"
    )

    # --------------------------------------------------------- filter complex
    # Build the doubled strip by vstacking two independent copies of the 4-quad
    # strip (inputs 0-3 and inputs 4-7 — the same files passed twice on the
    # command line).  This avoids the split filter which causes a silent
    # AVERROR(EINVAL) on some Windows ffmpeg builds for portrait resolutions.
    #
    # The doubled strip (2×strip_h) is needed for seamless looping: the y
    # expression from _variable_scroll_y() returns values in [0, strip_h), so
    # the crop window (out_h < strip_h) can extend past strip_h when y is near
    # the top of the range.  Doubling ensures y + out_h < 2*strip_h always.
    #
    # The crop filter re-evaluates x/y expressions for every frame by default
    # (unlike scale/pad which evaluate w/h only at init).  No eval option needed.
    y_expr   = _variable_scroll_y(strip_h, speed, duration)
    top_in   = ''.join(f"[{i}:v]" for i in range(4))    # inputs 0-3
    bot_in   = ''.join(f"[{i}:v]" for i in range(4, 8)) # inputs 4-7 (same files again)
    filt = (
        f"{top_in}vstack=4[top];"
        f"{bot_in}vstack=4[bot];"
        f"[top][bot]vstack[loop];"
        f"[loop]crop=w={src_w}:h={out_h}:x=0:y={y_expr}[out]"
    )

    # ------------------------------------------------------- ffmpeg command
    temp = out_path.parent / f'{base}_temp.mp4'
    out_path.parent.mkdir(parents=True, exist_ok=True)

    seek_args = ['-ss', str(seek)] if seek else []
    tlim = ['-t', str(trial_run)] if trial_run else []
    # Use -progress pipe:2 (writes key=value blocks to stderr regardless of
    # -loglevel) instead of -stats (suppressed by -loglevel warning on newer
    # ffmpeg builds, causing run_ffmpeg to block indefinitely on read(1)).
    cmd = ['-y', '-hide_banner', '-loglevel', 'warning', '-progress', 'pipe:2', '-nostats']
    for q in quads:
        cmd += seek_args + ['-i', str(q)]   # inputs 0-3
    for q in quads:
        cmd += seek_args + ['-i', str(q)]   # inputs 4-7 (second copy for loop)
    # -itsoffset delays the audio input's timestamps by _AUDIO_DELAY_S so it
    # plays later, compensating for audio arriving ~200 ms ahead of video.
    audio_offset = ['-itsoffset', str(_AUDIO_DELAY_S)] if _AUDIO_DELAY_S else []
    cmd += audio_offset + seek_args + ['-i', str(fullset_wav)]  # input 8
    # Write the filter_complex to a temp file and use -filter_complex_script.
    # The burst-schedule y expression can exceed 40 000 characters for long
    # shows, which pushes the full command past Windows CreateProcess's 32 767-
    # character limit and causes ffmpeg to exit with AVERROR(EINVAL) before
    # encoding a single frame.  A script file sidesteps that limit entirely.
    filt_script = tempfile.NamedTemporaryFile(
        mode='w', suffix='.txt', delete=False, encoding='utf-8',
    )
    filt_path = pathlib.Path(filt_script.name)
    rc = -1
    t0 = time.monotonic()
    try:
        filt_script.write(filt)
        filt_script.close()
        logger.debug(
            f'REEL  filter_complex: {filt_path}  ({len(filt)} chars)',
            extra={'tui': False},
        )

        # --- ScriptRunner path ---
        if script_runner is not None:
            from nofun.script_runner import ScriptJob
            # base is out_path.stem e.g. '26-04-07_PRIZE.0_reel'.
            # generate_reel.py constructs quad paths as '{base}_{quad}.mp4' and
            # the output as '{base}_reel.mp4', so it needs the stem WITHOUT '_reel'.
            quad_base = base[:-5] if base.endswith('_reel') else base
            job = ScriptJob(
                script='generate_reel',
                args={
                    'quads-dir':     str(out_path.parent),
                    'base':          quad_base,
                    'audio-path':    str(fullset_wav),
                    'dest-dir':      str(out_path.parent),
                    'filter-script': str(filt_path),
                    'delay-ms':      str(_AUDIO_DELAY_S * 1000),
                    'trial':         str(trial_run),
                    'seek':          str(seek),
                },
                label=f'REEL  {base}',
            )
            logger.debug(f'REEL  ScriptRunner: generate_reel  {base}', extra={'tui': False})
            result = script_runner.run(job, progress_cb=progress_cb, proc_cb=proc_cb)
            rc = result.exit_code
            return rc == 0
        # --- Legacy run_ffmpeg path (no ScriptRunner) ---

        # Use -/filter_complex (the non-deprecated form) instead of
        # -filter_complex_script (which ffmpeg now warns about).
        cmd += [f'-/filter_complex', str(filt_path)]

        cmd += ['-map', '[out]', '-map', '8:a']
        # yuv420p: explicitly normalise pixel format before the encoder so
        # hardware encoders (h264_amf, h264_videotoolbox) don't reject the frame.
        cmd += ['-pix_fmt', 'yuv420p']
        # h264_amf (AMD GPU encoder) silently rejects portrait resolutions with
        # AVERROR(EINVAL) — no error message, just rc=-22.  Reel is a single
        # software-filtered pass that doesn't benefit from GPU hw-encode anyway,
        # so always use libx264 here regardless of the platform encoder config.
        cmd += ['-c:v', 'libx264', '-preset', 'fast', '-crf', '23']
        cmd += ['-c:a', 'aac', '-b:a', '192k']
        cmd += ['-shortest']
        cmd += tlim
        cmd += [str(temp)]

        logger.debug(f'REEL  cmd: {" ".join(cmd)}', extra={'tui': False})

        # Debug: log spawn and first-frame to confirm progress_cb is firing
        _first_frame = [True]
        _orig_progress_cb = progress_cb
        def _progress_cb_debug(frame: str, fps: str, tc: str, speed: str) -> None:
            if _first_frame[0]:
                logger.debug(f'REEL  first frame received (frame={frame})',
                             extra={'tui': False})
                _first_frame[0] = False
            if _orig_progress_cb:
                _orig_progress_cb(frame, fps, tc, speed)

        _orig_proc_cb = proc_cb
        def _proc_cb_debug(p: subprocess.Popen) -> None:
            logger.debug(f'REEL  ffmpeg spawned pid={p.pid}', extra={'tui': False})
            if _orig_proc_cb:
                _orig_proc_cb(p)

        t0 = time.monotonic()  # reset after filter write overhead
        rc = run_ffmpeg(cmd, logger, label=f'REEL  {base}',
                       progress_cb=_progress_cb_debug, proc_cb=_proc_cb_debug)
        logger.debug(f'REEL  ffmpeg exited rc={rc}  ({time.monotonic() - t0:.1f}s)',
                     extra={'tui': False})
    finally:
        filt_path.unlink(missing_ok=True)

    if rc == 0:
        # Use replace() not rename(): replace() is atomic and overwrites an
        # existing destination on both Unix and Windows (rename raises
        # FileExistsError on Windows if the target already exists).
        logger.debug(f'REEL  replacing {temp.name} → {out_path.name}',
                     extra={'tui': False})
        temp.replace(out_path)
        logger.debug(f'REEL  replace done', extra={'tui': False})
        elapsed = time.monotonic() - t0
        try:
            sz = out_path.stat().st_size
            logger.info(
                f"CREATE  {out_path.name}  ({fmt_size(sz)}, {elapsed:.0f}s)",
                extra={
                    'path':    str(out_path),
                    'size':    fmt_size(sz),
                    'elapsed': f"{elapsed:.0f}s",
                },
            )
        except OSError:
            pass
        return True

    temp.unlink(missing_ok=True)
    logger.error(f"REEL  encode failed for {base}")
    return False
