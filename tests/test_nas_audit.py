"""Tests for scripts/nas_audit.py — the read-only NAS deliverable integrity audit.

The verdict logic (`classify`) is a pure function over a record + probe, so the bulk
of coverage needs no disk and no ffmpeg. A few integration tests exercise the real
disk probe (`probe_perf`, `build_perfs`); the `--deep` CRC/decode tests need ffmpeg
and are skipped when it is absent.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import zipfile

import pytest

from scripts.nas_audit import (
    PerfRecord, Probe, build_perfs, classify, main, probe_perf,
)

DUMMY = pathlib.Path('z.zip')  # classify only checks nas_zip/d_zip for None-ness


# ---------------------------------------------------------------------------
# Pure verdict logic — no disk
# ---------------------------------------------------------------------------

class TestClassify:
    def test_ok(self):
        rec = PerfRecord('26-01-01_Band', nas_zip=DUMMY)
        v, _ = classify(rec, Probe(nas_size=100, openable=True, entries=20, chans=20))
        assert v == 'OK'

    def test_missing_when_no_zip_anywhere(self):
        rec = PerfRecord('26-01-01_Band')
        v, _ = classify(rec, Probe())
        assert v == 'MISSING'

    def test_cross_tier_gap_when_d_only(self):
        rec = PerfRecord('26-01-01_Band', d_zip=DUMMY)
        v, _ = classify(rec, Probe(d_size=100))
        assert v == 'CROSS_TIER_GAP'

    def test_zero_bytes(self):
        rec = PerfRecord('26-01-01_Band', nas_zip=DUMMY)
        v, _ = classify(rec, Probe(nas_size=0))
        assert v == 'ZERO_BYTES'

    def test_unopenable(self):
        rec = PerfRecord('26-01-01_Band', nas_zip=DUMMY)
        v, d = classify(rec, Probe(nas_size=100, openable=False, error='BadZipFile: x'))
        assert v == 'UNOPENABLE'
        assert 'BadZipFile' in d

    def test_crc_fail_only_in_deep(self):
        rec = PerfRecord('26-01-01_Band', nas_zip=DUMMY)
        deep = Probe(nas_size=100, openable=True, deep=True, crc_bad='ch3.flac')
        assert classify(rec, deep)[0] == 'CRC_FAIL'
        # same probe without deep flag must NOT report CRC_FAIL
        shallow = Probe(nas_size=100, openable=True, entries=4, chans=4, crc_bad='ch3.flac')
        assert classify(rec, shallow)[0] == 'OK'

    def test_decode_fail(self):
        rec = PerfRecord('26-01-01_Band', nas_zip=DUMMY)
        v, _ = classify(rec, Probe(nas_size=100, openable=True, deep=True,
                                   decode_bad='ch3.flac'))
        assert v == 'DECODE_FAIL'

    def test_channel_incomplete_is_egregious_only(self):
        rec = PerfRecord('26-01-01_Band', nas_zip=DUMMY,
                         d_wavs=[pathlib.Path(f'w{i}.wav') for i in range(32)])
        # 3/32 — egregious (HELLSEEKER) → hard fail
        v, _ = classify(rec, Probe(nas_size=100, openable=True, chans=3))
        assert v == 'CHANNEL_INCOMPLETE'

    def test_mild_shortfall_is_review_not_hard(self):
        rec = PerfRecord('26-01-01_Band', nas_zip=DUMMY,
                         d_wavs=[pathlib.Path(f'w{i}.wav') for i in range(20)])
        # 16/20 — above the quarter; silence-drop is legitimate → review only
        v, _ = classify(rec, Probe(nas_size=100, openable=True, chans=16))
        assert v == 'OK_REVIEW'

    def test_full_channels_is_ok(self):
        rec = PerfRecord('26-01-01_Band', nas_zip=DUMMY,
                         d_wavs=[pathlib.Path(f'w{i}.wav') for i in range(20)])
        v, _ = classify(rec, Probe(nas_size=100, openable=True, entries=20, chans=20))
        assert v == 'OK'

    def test_parity_mismatch(self):
        rec = PerfRecord('26-01-01_Band', nas_zip=DUMMY, d_zip=DUMMY)
        v, _ = classify(rec, Probe(nas_size=100, d_size=1000, openable=True, chans=20))
        assert v == 'PARITY_MISMATCH'

    def test_parity_within_band_is_ok(self):
        rec = PerfRecord('26-01-01_Band', nas_zip=DUMMY, d_zip=DUMMY)
        v, _ = classify(rec, Probe(nas_size=900, d_size=1000, openable=True,
                                   entries=20, chans=20))
        assert v == 'OK'


# ---------------------------------------------------------------------------
# Disk probe + perf-universe construction
# ---------------------------------------------------------------------------

def _zip(path: pathlib.Path, names: list[str], data: bytes = b'x' * 2048) -> pathlib.Path:
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        for n in names:
            z.writestr(n, data)
    return path


class TestProbeAndBuild:
    def test_probe_healthy(self, tmp_path):
        z = _zip(tmp_path / 'p_MULTITRACK.zip', ['a.flac', 'b.flac', 'notes.txt'])
        p = probe_perf(PerfRecord('p', nas_zip=z), deep=False)
        assert p.openable and p.entries == 3 and p.chans == 2
        assert p.nas_size > 0

    def test_probe_zero_bytes(self, tmp_path):
        z = tmp_path / 'p_MULTITRACK.zip'
        z.write_bytes(b'')
        p = probe_perf(PerfRecord('p', nas_zip=z), deep=False)
        assert p.nas_size == 0 and not p.openable

    def test_probe_garbage_is_unopenable(self, tmp_path):
        z = tmp_path / 'p_MULTITRACK.zip'
        z.write_bytes(b'this is not a zip file' * 10)
        p = probe_perf(PerfRecord('p', nas_zip=z), deep=False)
        assert not p.openable and p.error

    def test_build_perfs_unions_all_sources(self, tmp_path):
        nas = tmp_path / 'nas_audio'
        dzip = tmp_path / 'd_audio'
        darch = tmp_path / 'd_archive'
        for d in (nas, dzip, darch):
            d.mkdir()
        _zip(nas / '26-01-01_Alpha_MULTITRACK.zip', ['a.flac'])
        _zip(dzip / '26-01-02_Beta_MULTITRACK.zip', ['b.flac'])   # D: only — gap
        (darch / '26-01-03_Gamma_chan1.wav').write_bytes(b'\0')   # raw only — orphan

        perfs = build_perfs(nas, dzip, darch)

        assert set(perfs) == {'26-01-01_Alpha', '26-01-02_Beta', '26-01-03_Gamma'}
        assert perfs['26-01-01_Alpha'].nas_zip is not None
        assert perfs['26-01-02_Beta'].nas_zip is None and perfs['26-01-02_Beta'].d_zip is not None
        assert len(perfs['26-01-03_Gamma'].d_wavs) == 1


# ---------------------------------------------------------------------------
# main(): exit code + report sinks
# ---------------------------------------------------------------------------

class TestMain:
    def test_hard_fail_sets_exit_1_and_writes_json(self, tmp_path):
        nas = tmp_path / 'nas_audio'
        nas.mkdir()
        (nas / '26-01-01_Bad_MULTITRACK.zip').write_bytes(b'')   # ZERO_BYTES → hard fail
        out_json = tmp_path / 'out.json'

        rc = main(['--nas-audio', str(nas), '--json', str(out_json)])

        assert rc == 1
        rows = json.loads(out_json.read_text())
        assert rows[0]['verdict'] == 'ZERO_BYTES'

    def test_all_ok_sets_exit_0(self, tmp_path):
        nas = tmp_path / 'nas_audio'
        nas.mkdir()
        _zip(nas / '26-01-01_Good_MULTITRACK.zip', ['a.flac', 'b.flac'])

        rc = main(['--nas-audio', str(nas)])

        assert rc == 0


# ---------------------------------------------------------------------------
# --deep: CRC + FLAC decode (needs ffmpeg)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='ffmpeg not available')
class TestDeep:
    def _real_flac_bytes(self, tmp_path) -> bytes:
        from tests.test_flac_zip import _real_wav
        from nofun.audio import _encode_one_flac
        wav = _real_wav(tmp_path / 'ch.wav', seed=2)
        _arc, flac_bytes, _sz, _crc = _encode_one_flac(wav)
        return flac_bytes

    def test_deep_clean_flac_is_ok(self, tmp_path):
        z = tmp_path / 'p_MULTITRACK.zip'
        with zipfile.ZipFile(z, 'w', zipfile.ZIP_STORED) as zf:
            zf.writestr('ch1.flac', self._real_flac_bytes(tmp_path))
        rec = PerfRecord('p', nas_zip=z)
        p = probe_perf(rec, deep=True)
        assert p.deep and p.crc_bad is None and p.decode_bad is None
        assert classify(rec, p)[0] == 'OK'

    def test_deep_undecodable_flac_flagged(self, tmp_path):
        # Valid zip + correct CRC, but the .flac member is garbage → decode fails.
        z = tmp_path / 'p_MULTITRACK.zip'
        with zipfile.ZipFile(z, 'w', zipfile.ZIP_STORED) as zf:
            zf.writestr('ch1.flac', b'not really flac data' * 64)
        rec = PerfRecord('p', nas_zip=z)
        p = probe_perf(rec, deep=True)
        assert p.crc_bad is None       # CRC matches the stored garbage
        assert p.decode_bad == 'ch1.flac'
        assert classify(rec, p)[0] == 'DECODE_FAIL'

    def test_deep_crc_corruption_flagged(self, tmp_path):
        z = tmp_path / 'p_MULTITRACK.zip'
        with zipfile.ZipFile(z, 'w', zipfile.ZIP_STORED) as zf:
            zf.writestr('ch1.flac', b'A' * 4096)
        # Corrupt a byte inside the stored member payload (not the headers) so the
        # central-directory CRC no longer matches → testzip() catches it.
        raw = bytearray(z.read_bytes())
        mid = raw.find(b'A' * 16)
        assert mid != -1
        raw[mid + 8] ^= 0xFF
        z.write_bytes(raw)

        p = probe_perf(PerfRecord('p', nas_zip=z), deep=True)
        assert p.crc_bad == 'ch1.flac'
        assert classify(PerfRecord('p', nas_zip=z), p)[0] == 'CRC_FAIL'
