"""Tests for nofun/multiplex.py — content-based 1×1 vs 2×2 layout detection."""

import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

from nofun import multiplex
from nofun.multiplex import Layout, detect_layout, route_by_layout, _axis_ratios

GRID = 160
MID  = GRID // 2


def _buf(fn) -> bytes:
    """Build a GRID×GRID grayscale buffer from fn(row, col) -> 0..255."""
    return bytes(fn(r, c) & 0xFF for r in range(GRID) for c in range(GRID))


# ---------------------------------------------------------------------------
# _axis_ratios — the pure-Python seam math
# ---------------------------------------------------------------------------

class TestAxisRatios:
    def test_vertical_seam_only(self) -> None:
        """Left/right split → high vertical ratio, ~zero horizontal."""
        buf = _buf(lambda r, c: 50 if c < MID else 200)
        rv, rh = _axis_ratios(buf, GRID)
        assert rv > 10.0
        assert rh < 1.0

    def test_horizontal_seam_only(self) -> None:
        """Top/bottom split → high horizontal ratio, ~zero vertical."""
        buf = _buf(lambda r, c: 50 if r < MID else 200)
        rv, rh = _axis_ratios(buf, GRID)
        assert rh > 10.0
        assert rv < 1.0

    def test_both_seams_quad(self) -> None:
        """Four distinct quadrants → both ratios high."""
        def f(r, c):
            top, left = r < MID, c < MID
            return {(True, True): 30, (True, False): 120,
                    (False, True): 200, (False, False): 90}[(top, left)]
        rv, rh = _axis_ratios(_buf(f), GRID)
        assert rv > 10.0
        assert rh > 10.0

    def test_smooth_gradient_no_seam(self) -> None:
        """A smooth horizontal gradient has no midline spike."""
        buf = _buf(lambda r, c: c * 255 // (GRID - 1))
        rv, rh = _axis_ratios(buf, GRID)
        assert rv < 1.8
        assert rh < 1.8


# ---------------------------------------------------------------------------
# detect_layout — classification (ffmpeg stubbed out)
# ---------------------------------------------------------------------------

class TestDetectLayout:
    def _patch(self, monkeypatch, buf, dur=120.0) -> None:
        monkeypatch.setattr(multiplex, 'probe_format', lambda *a, **k: str(dur))
        monkeypatch.setattr(multiplex, '_sample_gray', lambda p, ts, g: buf)

    def test_quad(self, monkeypatch) -> None:
        def f(r, c):
            top, left = r < MID, c < MID
            return {(True, True): 30, (True, False): 120,
                    (False, True): 200, (False, False): 90}[(top, left)]
        self._patch(monkeypatch, _buf(f))
        assert detect_layout(Path('x.mov')) is Layout.QUAD_2x2

    def test_single(self, monkeypatch) -> None:
        self._patch(monkeypatch, _buf(lambda r, c: c * 255 // (GRID - 1)))
        assert detect_layout(Path('x.mov')) is Layout.SINGLE_1x1

    def test_side_by_side(self, monkeypatch) -> None:
        self._patch(monkeypatch, _buf(lambda r, c: 50 if c < MID else 200))
        assert detect_layout(Path('x.mov')) is Layout.SIDE_BY_SIDE

    def test_stacked(self, monkeypatch) -> None:
        self._patch(monkeypatch, _buf(lambda r, c: 50 if r < MID else 200))
        assert detect_layout(Path('x.mov')) is Layout.STACKED

    def test_zero_duration_unknown(self, monkeypatch) -> None:
        monkeypatch.setattr(multiplex, 'probe_format', lambda *a, **k: '0')
        assert detect_layout(Path('x.mov')) is Layout.UNKNOWN

    def test_all_samples_fail_unknown(self, monkeypatch) -> None:
        monkeypatch.setattr(multiplex, 'probe_format', lambda *a, **k: '120')
        monkeypatch.setattr(multiplex, '_sample_gray', lambda p, ts, g: None)
        assert detect_layout(Path('x.mov')) is Layout.UNKNOWN


# ---------------------------------------------------------------------------
# route_by_layout — list reassignment
# ---------------------------------------------------------------------------

class TestRouteByLayout:
    def _run(self, layouts: dict, pre_singles=None):
        perf_mov = defaultdict(list)
        for key, paths in layouts.items():
            for p, _ in paths:
                perf_mov[key].append(p)
        perf_singles = defaultdict(list)
        for key, vals in (pre_singles or {}).items():
            perf_singles[key].extend(vals)
        verdict = {p: lay for paths in layouts.values() for p, lay in paths}
        route_by_layout(perf_mov, perf_singles, lambda m: verdict[m])
        return perf_mov, perf_singles

    def test_single_moves_to_singles(self) -> None:
        p = Path('26-01-01_A.mov')
        mov, singles = self._run({'26-01-01_A': [(p, Layout.SINGLE_1x1)]})
        assert '26-01-01_A' not in mov          # last mov removed → key dropped
        assert singles['26-01-01_A'] == [p]

    def test_quad_stays(self) -> None:
        p = Path('26-01-01_A.mov')
        mov, singles = self._run({'26-01-01_A': [(p, Layout.QUAD_2x2)]})
        assert mov['26-01-01_A'] == [p]
        assert singles['26-01-01_A'] == []

    def test_unknown_stays(self) -> None:
        p = Path('26-01-01_A.mov')
        mov, singles = self._run({'26-01-01_A': [(p, Layout.UNKNOWN)]})
        assert mov['26-01-01_A'] == [p]
        assert singles['26-01-01_A'] == []

    def test_side_and_stacked_move_to_singles(self) -> None:
        ps = Path('s.mov')
        pt = Path('t.mov')
        mov, singles = self._run({'k': [(ps, Layout.SIDE_BY_SIDE),
                                        (pt, Layout.STACKED)]})
        assert 'k' not in mov
        assert set(singles['k']) == {ps, pt}

    def test_folder_singles_untouched(self) -> None:
        quad = Path('q.mov')
        folder_single = Path('Singles/f.mov')
        mov, singles = self._run(
            {'k': [(quad, Layout.QUAD_2x2)]},
            pre_singles={'k': [folder_single]},
        )
        assert mov['k'] == [quad]
        assert singles['k'] == [folder_single]   # detection never ran on it

    def test_mixed_keeps_quad_diverts_single(self) -> None:
        q = Path('q.mov')
        s = Path('s.mov')
        mov, singles = self._run({'k': [(q, Layout.QUAD_2x2),
                                        (s, Layout.SINGLE_1x1)]})
        assert mov['k'] == [q]
        assert singles['k'] == [s]


# ---------------------------------------------------------------------------
# End-to-end with real ffmpeg (skipped if ffmpeg is unavailable)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='ffmpeg not installed')
class TestFfmpegIntegration:
    def _make(self, path: Path, filter_complex: str, inputs: list[str]) -> None:
        cmd = ['ffmpeg', '-y', '-v', 'error']
        for inp in inputs:
            cmd += ['-f', 'lavfi', '-i', inp]
        cmd += ['-filter_complex', filter_complex, '-map', '[out]',
                '-frames:v', '60', '-pix_fmt', 'yuv420p', str(path)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)

    def test_real_quad_detected(self, tmp_path) -> None:
        out = tmp_path / 'quad.mp4'
        self._make(
            out,
            '[0][1]hstack[t];[2][3]hstack[b];[t][b]vstack,format=gray[out]',
            ['color=c=black:s=320x180:r=30:d=2',
             'color=c=0x404040:s=320x180:r=30:d=2',
             'color=c=0xA0A0A0:s=320x180:r=30:d=2',
             'color=c=white:s=320x180:r=30:d=2'],
        )
        assert detect_layout(out, n_frames=3) is Layout.QUAD_2x2

    def test_real_single_detected(self, tmp_path) -> None:
        out = tmp_path / 'single.mp4'
        self._make(
            out,
            # colours pinned — gradients defaults to random colours per run,
            # and some draws produce an edge the seam detector reads as a quad
            'gradients=s=640x360:r=30:d=2:c0=black:c1=white:nb_colors=2,'
            'format=gray[out]',
            [],
        )
        # A smooth full-frame gradient has no internal seam.
        assert detect_layout(out, n_frames=3) is Layout.SINGLE_1x1
