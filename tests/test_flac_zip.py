"""Tests for the WAV→FLAC-in-zip migration.

Covers the lossless round-trip through `_create_and_verify_zip` (the bundle now
holds STORED `.flac` entries whose decoded PCM matches the source WAV) and the
gated `scripts/backfill_flac_zips.py` convert/skip/verify-fail behaviour.

These exercise real ffmpeg FLAC encoding, so they're skipped when ffmpeg is
absent.
"""

from __future__ import annotations

import io
import math
import pathlib
import shutil
import struct
import wave
import zipfile

import pytest

if shutil.which('ffmpeg') is None:
    pytest.skip('ffmpeg not available', allow_module_level=True)

from scripts.backfill_flac_zips import _convert_one, _decoded_md5


def _real_wav(path: pathlib.Path, *, frames: int = 48000, rate: int = 48000,
              seed: int = 0) -> pathlib.Path:
    """Write a valid 16-bit mono WAV holding a tone (FLAC-compressible, like real audio).

    A pure-ish sine is what FLAC models well — the synthetic equivalent of real
    instrument audio — so the bundle genuinely shrinks, as it does in prod.
    """
    freq = 220.0 + seed * 55.0
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b''.join(
            struct.pack('<h', int(12000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(frames)
        ))
    path.write_bytes(buf.getvalue())
    return path


def _real_wav32(path: pathlib.Path, *, frames: int = 48000, rate: int = 48000,
                seed: int = 0) -> pathlib.Path:
    """Write a 32-bit mono WAV whose low 8 bits are genuinely populated.

    The recorder emits pcm_s32le. We deliberately set the low byte of every
    sample so a 24-bit FLAC re-encode is provably lossy there — the fixture for
    the accepted-24-bit-FLAC behaviour (see TestThirtyTwoBitTruncation).
    """
    freq = 220.0 + seed * 55.0
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(4)
        w.setframerate(rate)
        w.writeframes(b''.join(
            struct.pack('<i',
                        (int(0.3 * 2**31 * math.sin(2 * math.pi * freq * i / rate))
                         & ~0xFF) | (i & 0xFF))
            for i in range(frames)
        ))
    path.write_bytes(buf.getvalue())
    return path


def _make_wav_zip(zip_path: pathlib.Path, chans: list[pathlib.Path]) -> None:
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for c in chans:
            z.write(c, arcname=c.name)


# ---------------------------------------------------------------------------
# Lossless round-trip through the live zip builder
# ---------------------------------------------------------------------------

class TestFlacZipRoundTrip:
    def _fake_audio(self, tmp_path):
        from tests.test_audio import _FakeAudio
        return _FakeAudio(tmp_path)

    def test_zip_holds_stored_flac(self, tmp_path):
        fa = self._fake_audio(tmp_path)
        ch = _real_wav(tmp_path / '26-01-01_Band_chan1.wav')
        zip_path = tmp_path / 'out.zip'

        ok, dropped = fa._create_and_verify_zip(zip_path, [ch])

        assert ok and not dropped
        with zipfile.ZipFile(zip_path) as z:
            info = z.getinfo('26-01-01_Band_chan1.flac')
            assert info.compress_type == zipfile.ZIP_STORED

    def test_decoded_pcm_is_lossless(self, tmp_path):
        """FLAC in the bundle decodes to PCM identical to the source WAV."""
        fa = self._fake_audio(tmp_path)
        ch = _real_wav(tmp_path / '26-01-01_Band_chan1.wav', seed=3)
        src_md5 = _decoded_md5(ch)
        zip_path = tmp_path / 'out.zip'

        fa._create_and_verify_zip(zip_path, [ch])

        out = tmp_path / 'extracted'
        out.mkdir()
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(out)
        flac = out / '26-01-01_Band_chan1.flac'
        assert _decoded_md5(flac) == src_md5

    def test_flac_smaller_than_stored_wav(self, tmp_path):
        fa = self._fake_audio(tmp_path)
        ch = _real_wav(tmp_path / '26-01-01_Band_chan1.wav', frames=48000)
        zip_path = tmp_path / 'out.zip'

        fa._create_and_verify_zip(zip_path, [ch])

        with zipfile.ZipFile(zip_path) as z:
            flac_size = z.getinfo('26-01-01_Band_chan1.flac').file_size
        assert flac_size < ch.stat().st_size


# ---------------------------------------------------------------------------
# Backfill: convert / skip / verify-fail
# ---------------------------------------------------------------------------

class TestBackfill:
    def test_converts_wav_zip_to_flac(self, tmp_path):
        c1 = _real_wav(tmp_path / 'chan1.wav', seed=1)
        c2 = _real_wav(tmp_path / 'chan2.wav', seed=2)
        zip_path = tmp_path / 'show_MULTITRACK.zip'
        _make_wav_zip(zip_path, [c1, c2])
        orig_md5 = {'chan1': _decoded_md5(c1), 'chan2': _decoded_md5(c2)}

        changed, old_b, new_b = _convert_one(zip_path, apply=True)

        # Size savings is a real-audio/prod property (verified end-to-end), not a
        # unit invariant — a pure synthetic tone can deflate better than it FLACs.
        # Here we assert the lossless, STORED-.flac conversion itself.
        assert changed
        assert new_b > 0
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
            assert names == {'chan1.flac', 'chan2.flac'}
            assert all(z.getinfo(n).compress_type == zipfile.ZIP_STORED
                       for n in names)
            # decoded PCM still matches the pre-backfill source
            out = tmp_path / 'x'
            out.mkdir()
            z.extractall(out)
        assert _decoded_md5(out / 'chan1.flac') == orig_md5['chan1']
        assert _decoded_md5(out / 'chan2.flac') == orig_md5['chan2']

    def test_converts_production_named_members(self, tmp_path):
        """Engine WAVs are '<perf>_chanN.wav' — the glob must match those, not
        just names that literally start with 'chan' (the bug that made the
        2026-06-12 prod dry run skip every real zip)."""
        c1 = _real_wav(tmp_path / '26-01-01_Band_chan1.wav', seed=1)
        zip_path = tmp_path / '26-01-01_Band_MULTITRACK.zip'
        _make_wav_zip(zip_path, [c1])

        changed, _old, _new = _convert_one(zip_path, apply=True)

        assert changed
        with zipfile.ZipFile(zip_path) as z:
            assert z.namelist() == ['26-01-01_Band_chan1.flac']

    def test_bad_zip_skipped_without_crash(self, tmp_path):
        """A corrupt/truncated zip is reported and left untouched — the batch
        must keep going (prod has known-bad ZIP64 zips in the same tree)."""
        bad = tmp_path / 'bad_MULTITRACK.zip'
        bad.write_bytes(b'not a zip at all')

        changed, old_b, new_b = _convert_one(bad, apply=True)

        assert not changed
        assert old_b == new_b
        assert bad.read_bytes() == b'not a zip at all'

    def test_mixed_zip_left_untouched(self, tmp_path):
        """A zip holding chan WAVs plus any other member is skipped — rewriting
        it would silently drop the extra member."""
        c1 = _real_wav(tmp_path / '26-01-01_Band_chan1.wav', seed=1)
        extra = tmp_path / 'notes.txt'
        extra.write_text('rig notes')
        zip_path = tmp_path / '26-01-01_Band_MULTITRACK.zip'
        _make_wav_zip(zip_path, [c1, extra])
        before = zip_path.read_bytes()

        changed, _old, _new = _convert_one(zip_path, apply=True)

        assert not changed
        assert zip_path.read_bytes() == before

    def test_dry_run_leaves_original_untouched(self, tmp_path):
        c1 = _real_wav(tmp_path / 'chan1.wav')
        zip_path = tmp_path / 'show_MULTITRACK.zip'
        _make_wav_zip(zip_path, [c1])
        before = zip_path.read_bytes()

        changed, _old, _new = _convert_one(zip_path, apply=False)

        assert changed                       # would convert
        assert zip_path.read_bytes() == before   # but didn't write
        with zipfile.ZipFile(zip_path) as z:
            assert z.namelist() == ['chan1.wav']

    def test_already_flac_zip_is_skipped(self, tmp_path):
        c1 = _real_wav(tmp_path / 'chan1.wav')
        wav_zip = tmp_path / 'tmp_MULTITRACK.zip'
        _make_wav_zip(wav_zip, [c1])
        # convert once so it holds .flac
        _convert_one(wav_zip, apply=True)
        before = wav_zip.read_bytes()

        changed, old_b, new_b = _convert_one(wav_zip, apply=True)

        assert not changed
        assert old_b == new_b
        assert wav_zip.read_bytes() == before   # untouched second time

    def test_md5_mismatch_leaves_original_intact(self, tmp_path, monkeypatch):
        c1 = _real_wav(tmp_path / 'chan1.wav')
        zip_path = tmp_path / 'show_MULTITRACK.zip'
        _make_wav_zip(zip_path, [c1])
        before = zip_path.read_bytes()

        # Force the per-channel verification to disagree.
        import scripts.backfill_flac_zips as bf
        calls = {'n': 0}

        def _fake_md5(path):
            calls['n'] += 1
            return f'deadbeef{calls["n"]}'   # never equal between wav and flac

        monkeypatch.setattr(bf, '_decoded_md5', _fake_md5)

        changed, old_b, new_b = bf._convert_one(zip_path, apply=True)

        assert not changed
        assert zip_path.read_bytes() == before   # never replaced


# ---------------------------------------------------------------------------
# 32-bit source → 24-bit FLAC: the accepted, documented lossiness
# ---------------------------------------------------------------------------

class TestThirtyTwoBitTruncation:
    """Locks in the 2026-06-07 decision: the recorder emits 32-bit PCM but FLAC
    caps at 24-bit, so WAV→FLAC keeps the top 24 bits bit-exact and drops the
    low 8. We accept this as the archive depth. These tests prove BOTH halves —
    the 24-bit content is preserved, and the low 8 bits really are gone — so the
    behaviour can't silently regress (e.g. a codec swap that mangles bits 17-24,
    or a verifier loosened back to 16-bit).
    """

    def test_source_is_really_32bit(self, tmp_path):
        w = _real_wav32(tmp_path / 'chan1.wav')
        with wave.open(str(w), 'rb') as r:
            assert r.getsampwidth() == 4   # 32-bit, as prod records

    def test_top_24_bits_preserved(self, tmp_path):
        """_decoded_md5 (24-bit) agrees between the 32-bit WAV and its FLAC."""
        from nofun.audio import _encode_one_flac
        wav = _real_wav32(tmp_path / 'chan1.wav')
        arcname, flac_bytes, _size, _crc = _encode_one_flac(wav)
        flac = tmp_path / arcname
        flac.write_bytes(flac_bytes)
        assert _decoded_md5(flac) == _decoded_md5(wav)

    def test_low_8_bits_are_dropped(self, tmp_path):
        """Decoded at full 32-bit, FLAC == WAV with the low byte zeroed."""
        import numpy as np
        import soundfile as sf
        from nofun.audio import _encode_one_flac
        wav = _real_wav32(tmp_path / 'chan1.wav')
        arcname, flac_bytes, _size, _crc = _encode_one_flac(wav)
        flac = tmp_path / arcname
        flac.write_bytes(flac_bytes)

        w32, _ = sf.read(str(wav), dtype='int32')
        f32, _ = sf.read(str(flac), dtype='int32')
        n = min(len(w32), len(f32))
        assert not np.array_equal(w32[:n], f32[:n])          # genuinely lossy
        assert np.array_equal(f32[:n], w32[:n] & ~0xFF)      # exactly the low byte
