"""Unit tests for inventory_generator.py"""

import pathlib

import pytest

from nofun.inventory import (
    PerformanceState,
    _clean_band,
    build_state_dashboard,
    build_performance_states,
    classify_file,
    classify_location,
    extract_date_band,
    extract_date_band_from_path,
    files_for_perf,
    perf_key,
    perf_output_name,
    rows_from_db,
    scan_files,
    short_date,
)


class TestExtractDateBand:
    @pytest.mark.parametrize("filename,exp_date,exp_band", [
        # Standard short-date format
        ("26-2-7_NoFun_DeadGowns_CAM1.mp4", "26-02-07", "NoFun_DeadGowns"),
        ("26-2-7_NoFun_DeadGowns_ch01.wav", "26-02-07", "NoFun_DeadGowns"),
        ("26-01-24 Film and Gender Audio.wav", "26-01-24", "Film_and_Gender"),
        # Audio recorder format
        ("R_20260207-143022.wav",           "2026-02-07", "Audio Recorder"),
        # Long-date format
        ("20260207_SomeBand.mov",           "26-02-07", "SomeBand"),
        # Unrecognised
        ("random_file.mp4",                "TBD",        "TBD"),
        # Zero-padded short date
        ("26-01-01_BandName.mov",          "26-01-01", "BandName"),
        # Pre-split channel WAVs from Audio/ subfolder
        ("26-3-11_DAISY_CHAIN_chan7.3.wav", "26-03-11", "DAISY_CHAIN"),
        ("26-3-11_DAISY_CHAIN_chan12.wav",  "26-03-11", "DAISY_CHAIN"),
        ("26-3-11_DAISY_CHAIN_chan7.wav",   "26-03-11", "DAISY_CHAIN"),
        ("26-3-11_DAISY_CHAIN_chan.wav",    "26-03-11", "DAISY_CHAIN"),   # no digits
    ])
    def test_formats(self, filename, exp_date, exp_band):
        date, band = extract_date_band(filename)
        assert date == exp_date, f"date mismatch for {filename}"
        assert band == exp_band, f"band mismatch for {filename}"


class TestPerfKey:
    """Canonical perf identity helper (nofun/inventory.py). Date axis only —
    band is assembled verbatim; band normalisation lives in extract_date_band."""

    def test_short_date_truncates_long(self):
        assert short_date('2026-05-25') == '26-05-25'

    def test_short_date_idempotent(self):
        assert short_date('26-05-25') == '26-05-25'

    def test_short_date_leaves_tbd(self):
        assert short_date('TBD') == 'TBD'

    def test_perf_key_normalises_long_date(self):
        assert perf_key('2026-05-25', 'ALTAR') == '26-05-25_ALTAR'

    def test_perf_key_idempotent_on_short(self):
        assert perf_key('25-05-25', 'ALTAR') == '25-05-25_ALTAR'

    def test_perf_key_passes_band_verbatim(self):
        # perf_key does NOT re-clean the band (decided 2026-05-31): a distinct
        # sub-performance suffix must survive untouched, never merged away.
        assert perf_key('26-03-14', 'THE_OBSESSED_ENCORE') == '26-03-14_THE_OBSESSED_ENCORE'
        assert perf_key('26-03-14', 'THE_OBSESSED_SOUNDCHECK') == '26-03-14_THE_OBSESSED_SOUNDCHECK'


class TestPerfOutputName:
    """Canonical final-output naming — the shared anchor producers and tests import."""

    def test_multitrack(self):
        assert perf_output_name('26-03-11_DAISY_CHAIN', 'multitrack') == \
            '26-03-11_DAISY_CHAIN_MULTITRACK.zip'

    def test_audio(self):
        assert perf_output_name('26-04-11_ALTAR', 'audio') == '26-04-11_ALTAR_AUDIO.mp3'

    def test_reel(self):
        assert perf_output_name('26-04-11_ALTAR', 'reel') == '26-04-11_ALTAR_INSTAGRAM.mp4'

    def test_quad_requires_cam(self):
        assert perf_output_name('26-04-11_ALTAR', 'quad', 'CAM2') == '26-04-11_ALTAR_CAM2.mp4'

    def test_unknown_kind_raises(self):
        with pytest.raises(KeyError):
            perf_output_name('26-04-11_ALTAR', 'poster')


class TestExtractDateBandFromPath:
    def test_falls_back_to_parent_folder(self, tmp_path):
        folder = tmp_path / '26-05-17_DAISY_CHAIN'
        folder.mkdir()
        f = folder / 'DAISY_CHAIN_UL.mp4'
        f.touch()
        assert extract_date_band_from_path(f) == ('26-05-17', 'DAISY_CHAIN')

    def test_uses_filename_when_present(self, tmp_path):
        f = tmp_path / '26-05-17_BAND_UL.mp4'
        f.touch()
        assert extract_date_band_from_path(f) == ('26-05-17', 'BAND')

    def test_tbd_when_neither_has_date(self, tmp_path):
        folder = tmp_path / 'random'
        folder.mkdir()
        f = folder / 'BAND_UL.mp4'
        f.touch()
        assert extract_date_band_from_path(f) == ('TBD', 'TBD')

    def test_multi_band_folder_recovers_file_band(self, tmp_path):
        # Folder collects multiple bands' files for the same date
        folder = tmp_path / '26-05-17_DAISY_CHAIN_OTHER_BAND'
        folder.mkdir()
        f = folder / 'OTHER_BAND.zip'
        f.touch()
        date, band = extract_date_band_from_path(f)
        assert date == '26-05-17'
        assert band == 'OTHER_BAND'


class TestCleanBand:
    @pytest.mark.parametrize("raw,expected", [
        ("BandName_UL",          "BandName"),
        ("BandName_ch01",        "BandName"),
        ("BandName.wav",         "BandName"),
        ("BandName_UR_2",        "BandName"),
        ("SomeBand",             "SomeBand"),
        ("",                     "TBD"),
        ("DAISY_CHAIN_chan7.3",  "DAISY_CHAIN"),
        ("DAISY_CHAIN_chan12",   "DAISY_CHAIN"),
        ("DAISY_CHAIN_chan",     "DAISY_CHAIN"),   # no digits after _chan
        # REMASTER output suffixes should be stripped
        ("MX_LONELY_FULLSET",   "MX_LONELY"),
        ("PFC_PRIZE_FULLSET",   "PFC_PRIZE"),
        ("PFC_PRIZE_reel",      "PFC_PRIZE"),
    ])
    def test_clean(self, raw, expected):
        assert _clean_band(raw) == expected

    def test_clean_band_strips_trailing_whitespace(self):
        """Regression for log_bugs.md #1 — trailing whitespace caused perpetual SYNC rename."""
        assert _clean_band("SARA ")   == "SARA"
        assert _clean_band("  SARA ") == "SARA"
        assert _clean_band("SARA")    == "SARA"

    def test_clean_band_normalizes_inner_spaces(self):
        """Regression for 5/13 show: 'B hvpie' literal space must become 'B_hvpie'."""
        assert _clean_band("B hvpie") == "B_hvpie"
        assert _clean_band("MALL GOTH") == "MALL_GOTH"
        assert _clean_band("THE BAND NAME") == "THE_BAND_NAME"

    def test_extract_date_band_with_space_in_name(self):
        """extract_date_band returns underscore-normalised band for spaced filenames."""
        date, band = extract_date_band("26-05-13_B hvpie_chan11.25")
        assert date == "26-05-13"
        assert ' ' not in band
        assert band == "B_hvpie"


class TestClassifyFile:
    @pytest.mark.parametrize("name,path_str,expected", [
        ("foo_CAM1.mp4",                  "/d/videos/foo_CAM1.mp4",      "quadrant"),
        ("foo_CAM4.mp4",                  "/d/videos/foo_CAM4.mp4",      "quadrant"),
        ("foo_CAM1_1.mp4",                "/d/clips/foo/foo_CAM1_1.mp4", "clip"),
        ("band.zip",                      "/d/audio/band.zip",            "zipped audio"),
        ("band_ch01.wav",                 "/c/source/band_ch01.wav",      "audio"),
        ("rec.mov",                       "/c/VenueLighting/rec.mov",     "raw video"),
        ("foo.mp4",                       "/d/quadrants/foo.mp4",         "quadrant"),
        ("foo.mp4",                       "/d/other/foo.mp4",             "re-encoded"),
        ("foo.mp4",                       "/d/trial_runs/clips/x/f.mp4",  "clip"),
        # REMASTER outputs must not be classified as generic audio/video
        ("26-04-07_MX_LONELY_AUDIO.mp3",  "/d/audio/26-04-07_MX_LONELY_AUDIO.mp3", "fullset audio"),
        ("26-04-07_PFC_PRIZE_AUDIO.mp3",  "/d/audio/26-04-07_PFC_PRIZE_AUDIO.mp3", "fullset audio"),
        ("26-04-07_PFC_PRIZE_INSTAGRAM.mp4", "/d/videos/26-04-07_PFC_PRIZE_INSTAGRAM.mp4", "reel video"),
    ])
    def test_classify(self, name, path_str, expected):
        assert classify_file(name, pathlib.Path(path_str)) == expected


class TestBuildDashboard:
    def _make_row(self, date, band, ftype, size_gb=0.5, location='archive'):
        return {
            'date': date, 'band': band, 'type': ftype, 'size_gb': size_gb,
            'fullpath': pathlib.Path('/d/fake/file.mp4'),
            'filename': 'fake.mp4',
            'location': location,
            'size': int(size_gb * 1_073_741_824),
        }

    def test_contains_header(self):
        rows = [self._make_row("2026-01-01", "TestBand", "quadrant")]
        dash = build_state_dashboard(rows, 1)
        assert "MEDIA INVENTORY" in dash

    def test_shows_band(self):
        rows = [self._make_row("2026-01-01", "CoolBand", "quadrant")]
        dash = build_state_dashboard(rows, 1)
        assert "CoolBand" in dash

    def test_detected_state(self):
        rows = [self._make_row("2026-01-01", "Band", "raw video", location='source')]
        dash = build_state_dashboard(rows, 1)
        assert "pending encode" in dash

    def test_incomplete_state(self):
        # Single quadrant (< 4) with no audio → INCOMPLETE
        rows = [self._make_row("2026-01-01", "Band", "quadrant")]
        dash = build_state_dashboard(rows, 1)
        assert "INCOMPLETE" in dash

    def test_audio_pending_state(self):
        rows = [self._make_row("2026-01-01", "Band", "audio", location='source')]
        dash = build_state_dashboard(rows, 1)
        assert "audio pending" in dash

    def test_complete_state(self):
        # 4 quads + 1 zip → COMPLETE (date >30 days ago, no cloud)
        rows = [
            self._make_row("2026-01-01", "Band", "quadrant"),
            self._make_row("2026-01-01", "Band", "quadrant"),
            self._make_row("2026-01-01", "Band", "quadrant"),
            self._make_row("2026-01-01", "Band", "quadrant"),
            self._make_row("2026-01-01", "Band", "zipped audio"),
        ]
        dash = build_state_dashboard(rows, 5)
        band_lines = [l for l in dash.splitlines() if "Band" in l and "26-01-01" in l]
        assert band_lines, "No band row found"
        assert "INCOMPLETE" not in band_lines[0]
        assert "complete" in band_lines[0].lower()

    def test_tbd_goes_to_clutter(self):
        rows = [self._make_row("TBD", "TBD", "raw video")]
        dash = build_state_dashboard(rows, 1)
        assert "Unclassified" in dash

    def test_audio_recorder_goes_to_clutter(self):
        rows = [self._make_row("2026-01-01", "Audio Recorder", "audio")]
        dash = build_state_dashboard(rows, 1)
        assert "Unclassified" in dash

    def test_file_count_in_footer(self):
        rows = [self._make_row("2026-01-01", "Band", "quadrant")]
        dash = build_state_dashboard(rows, 42)
        assert "42 files indexed" in dash


class TestScanFiles:
    def test_finds_media_files(self, tmp_path):
        (tmp_path / "a.mov").write_bytes(b'\x00')
        (tmp_path / "b.wav").write_bytes(b'\x00')
        (tmp_path / "c.txt").write_bytes(b'\x00')  # should be ignored
        results = list(scan_files([tmp_path]))
        names = {r['filename'] for r in results}
        assert 'a.mov' in names
        assert 'b.wav' in names
        assert 'c.txt' not in names

    def test_limit_works(self, tmp_path):
        for i in range(10):
            (tmp_path / f"f{i}.mp4").write_bytes(b'\x00')
        results = list(scan_files([tmp_path], limit=3))
        assert len(results) == 3

    def test_missing_path_skipped(self):
        results = list(scan_files([pathlib.Path('/nonexistent/path')]))
        assert results == []

    def test_yields_metadata_keys(self, tmp_path):
        (tmp_path / "x.mp4").write_bytes(b'\x00' * 100)
        results = list(scan_files([tmp_path]))
        assert len(results) == 1
        r = results[0]
        assert 'fullpath' in r
        assert 'filename' in r
        assert 'size' in r
        assert 'mtime' in r
        assert r['size'] == 100


class TestRowsFromDb:
    def test_empty_db_returns_empty(self, tmp_path):
        from nofun.encoding_db import EncodingDB
        db = EncodingDB(tmp_path / 'db.json')
        assert rows_from_db(db) == []

    def test_quadrant_record_returns_row(self, tmp_path):
        from nofun.encoding_db import EncodingDB
        db = EncodingDB(tmp_path / 'db.json')
        p  = tmp_path / 'videos' / '26-01-01_Band_UL.mp4'
        db.upsert('2026-01-01', 'Band', 'quadrant_video', {
            'path': str(p), 'size': 1000, 'mtime': 1700000000.0,
            'type': 'quadrant', 'location': 'archive',
        })
        result = rows_from_db(db)
        assert len(result) == 1
        r = result[0]
        assert r['date']     == '26-01-01'
        assert r['band']     == 'Band'
        assert r['type']     == 'quadrant'
        assert r['location'] == 'archive'
        assert r['fullpath'] == p

    def test_category_type_fallback(self, tmp_path):
        """Records without explicit 'type' fall back to category mapping."""
        from nofun.encoding_db import EncodingDB
        db = EncodingDB(tmp_path / 'db.json')
        p  = tmp_path / 'audio' / '26-01-01_Band.zip'
        db.upsert('2026-01-01', 'Band', 'zipped_audio', {
            'path': str(p), 'size': 500, 'mtime': 1700000000.0,
        })
        result = rows_from_db(db)
        assert result[0]['type'] == 'zipped audio'

    def test_unknown_category_skipped(self, tmp_path):
        from nofun.encoding_db import EncodingDB
        db = EncodingDB(tmp_path / 'db.json')
        db.upsert('2026-01-01', 'Band', 'unknown_category', {
            'path': str(tmp_path / 'x.bin'), 'size': 1, 'mtime': 0.0,
        })
        assert rows_from_db(db) == []

    def test_multiple_categories(self, tmp_path):
        from nofun.encoding_db import EncodingDB
        db = EncodingDB(tmp_path / 'db.json')
        for cat, fname in [
            ('quadrant_video', 'vid_UL.mp4'),
            ('zipped_audio',   'aud.zip'),
            ('raw_video',      'raw.mov'),
        ]:
            db.upsert('2026-01-01', 'Band', cat, {
                'path': str(tmp_path / fname), 'size': 1, 'mtime': 0.0,
            })
        result = rows_from_db(db)
        assert len(result) == 3
        types = {r['type'] for r in result}
        assert types == {'quadrant', 'zipped audio', 'raw video'}

    def test_clips_summary_uses_db_count(self, tmp_path):
        """rows_from_db yields one clip row using DB count/size (no per-file stat)."""
        from nofun.encoding_db import EncodingDB
        clip_dir = tmp_path / 'clips' / '26-03-20_BAND'
        clip_dir.mkdir(parents=True)

        db = EncodingDB(tmp_path / 'db.json')
        db.set_clips_summary('2026-03-20', 'BAND', {
            'dir': str(clip_dir), 'count': 2, 'codec': 'h264', 'total_size': 2100,
        })

        result = rows_from_db(db)
        clip_rows = [r for r in result if r['type'] == 'clip']
        assert len(clip_rows) == 1
        assert clip_rows[0]['date'] == '26-03-20'
        assert clip_rows[0]['band'] == 'BAND'
        assert clip_rows[0]['size'] == 2100

    def test_clips_summary_missing_dir_skipped(self, tmp_path):
        """If clips_summary dir does not exist on disk, no rows are returned."""
        from nofun.encoding_db import EncodingDB
        db = EncodingDB(tmp_path / 'db.json')
        db.set_clips_summary('2026-03-20', 'BAND', {
            'dir': str(tmp_path / 'nonexistent'), 'count': 5,
        })
        result = rows_from_db(db)
        assert result == []


class TestRecorderPatAmPm:
    """RECORDER_PAT must match filenames with am/pm suffix."""
    @pytest.mark.parametrize("filename,exp_date", [
        ("R_20260124-104704.wav",   "2026-01-24"),
        ("R_20260124-104704pm.wav", "2026-01-24"),
        ("R_20260124-104704am.wav", "2026-01-24"),
        ("R_20260124-104704PM.wav", "2026-01-24"),  # uppercase
    ])
    def test_recorder_with_am_pm(self, filename, exp_date):
        date, band = extract_date_band(filename)
        assert date == exp_date
        assert band == "Audio Recorder"


class TestClassifyLocation:
    @pytest.mark.parametrize("path_str,expected", [
        ("/c/Users/testuser/VenueLighting/foo.mov", "source"),
        ("/c/Users/testuser/OneDrive - No Fun Troy LLC/Multitracks/26-01/foo.zip", "cloud"),
        ("/d/videos/foo_UL.mp4", "archive"),
        ("/d/audio/foo.zip",     "archive"),
    ])
    def test_classify_location(self, path_str, expected):
        assert classify_location(pathlib.Path(path_str)) == expected


class TestPerformanceState:
    def _ps(self, **kwargs) -> PerformanceState:
        return PerformanceState(date='2025-01-01', band='TestBand', **kwargs)

    def test_state_detected_when_raw_movs_no_quads(self, tmp_path):
        f = tmp_path / 'foo.mov'; f.write_bytes(b'\x00')
        ps = self._ps(raw_movs=[f])
        assert ps.state == 'DETECTED'

    def test_state_audio_pending_when_wav_no_zip(self, tmp_path):
        f = tmp_path / 'foo.wav'; f.write_bytes(b'\x00')
        ps = self._ps(raw_wavs=[f])
        assert ps.state == 'AUDIO_PENDING'

    def test_state_complete_when_all_present(self, tmp_path):
        mov  = tmp_path / 'foo.mov'; mov.write_bytes(b'\x00')
        quads = [tmp_path / f'foo_{q}.mp4' for q in ('UL', 'UR', 'LL', 'LR')]
        for q in quads: q.write_bytes(b'\x00')
        zp   = tmp_path / 'foo.zip'; zp.write_bytes(b'\x00')
        ps = PerformanceState(
            date='2024-01-01', band='Band',
            mov_files=[mov], quad_files=quads, zip_files=[zp],
        )
        # age > 30, no cloud — should be COMPLETE
        assert ps.state == 'COMPLETE'

    def test_state_share_eligible_when_recent(self, tmp_path):
        import datetime
        today = datetime.date.today()
        date_str = today.strftime('%Y-%m-%d')
        mov  = tmp_path / 'foo.mov'; mov.write_bytes(b'\x00')
        quads = [tmp_path / f'foo_{q}.mp4' for q in ('UL', 'UR', 'LL', 'LR')]
        for q in quads: q.write_bytes(b'\x00')
        zp = tmp_path / 'foo.zip'; zp.write_bytes(b'\x00')
        ps = PerformanceState(
            date=date_str, band='Band',
            mov_files=[mov], quad_files=quads, zip_files=[zp],
        )
        assert ps.state == 'SHARE_ELIGIBLE'

    def test_nofun_short_not_share_eligible(self, tmp_path):
        """NoFun band + duration < 60s → COMPLETE, not SHARE_ELIGIBLE."""
        import datetime
        today = datetime.date.today()
        date_str = today.strftime('%Y-%m-%d')
        quads = [tmp_path / f'foo_{q}.mp4' for q in ('UL', 'UR', 'LL', 'LR')]
        for q in quads: q.write_bytes(b'\x00')
        zp = tmp_path / 'foo.zip'; zp.write_bytes(b'\x00')
        ps = PerformanceState(
            date=date_str, band='NoFun',
            quad_files=quads, zip_files=[zp], duration_sec=45.0,
        )
        assert ps.state == 'COMPLETE'

    def test_nofun_long_is_share_eligible(self, tmp_path):
        """NoFun band + duration >= 60s → still SHARE_ELIGIBLE."""
        import datetime
        today = datetime.date.today()
        date_str = today.strftime('%Y-%m-%d')
        quads = [tmp_path / f'foo_{q}.mp4' for q in ('UL', 'UR', 'LL', 'LR')]
        for q in quads: q.write_bytes(b'\x00')
        zp = tmp_path / 'foo.zip'; zp.write_bytes(b'\x00')
        ps = PerformanceState(
            date=date_str, band='NoFun',
            quad_files=quads, zip_files=[zp], duration_sec=90.0,
        )
        assert ps.state == 'SHARE_ELIGIBLE'

    def test_nofun_no_duration_is_share_eligible(self, tmp_path):
        """NoFun band + no duration info → SHARE_ELIGIBLE (can't determine, don't block)."""
        import datetime
        today = datetime.date.today()
        date_str = today.strftime('%Y-%m-%d')
        quads = [tmp_path / f'foo_{q}.mp4' for q in ('UL', 'UR', 'LL', 'LR')]
        for q in quads: q.write_bytes(b'\x00')
        zp = tmp_path / 'foo.zip'; zp.write_bytes(b'\x00')
        ps = PerformanceState(
            date=date_str, band='NoFun',
            quad_files=quads, zip_files=[zp],
        )
        assert ps.state == 'SHARE_ELIGIBLE'

    def test_state_complete_quads_no_original_mov(self, tmp_path):
        """Quads without original .mov should still reach COMPLETE (not INCOMPLETE)."""
        quads = [tmp_path / f'foo_{q}.mp4' for q in ('UL', 'UR', 'LL', 'LR')]
        for q in quads: q.write_bytes(b'\x00')
        zp = tmp_path / 'foo.zip'; zp.write_bytes(b'\x00')
        ps = PerformanceState(
            date='2024-01-01', band='Band',
            quad_files=quads, zip_files=[zp],
        )
        assert ps.state == 'COMPLETE'

    def test_state_audio_only_cloud_shared(self, tmp_path):
        """Audio-only show with ZIP in cloud (recent) → SHARED, not INCOMPLETE."""
        import datetime
        today = datetime.date.today()
        date_str = today.strftime('%Y-%m-%d')
        cloud_zip = tmp_path / 'foo.zip'; cloud_zip.write_bytes(b'\x00')
        ps = PerformanceState(date=date_str, band='Band', cloud_files=[cloud_zip])
        assert ps.state == 'SHARED'

    def test_state_audio_only_cloud_expired(self, tmp_path):
        """Audio-only show with old ZIP in cloud → SHARE_EXPIRED, not INCOMPLETE."""
        cloud_zip = tmp_path / 'foo.zip'; cloud_zip.write_bytes(b'\x00')
        ps = PerformanceState(date='2025-01-01', band='Band', cloud_files=[cloud_zip])
        assert ps.state == 'SHARE_EXPIRED'



class TestBuildPerformanceStates:
    def _row(self, date, band, ftype, location='archive', path=None):
        return {
            'date': date, 'band': band, 'type': ftype,
            'location': location,
            'fullpath': pathlib.Path(path or f'/d/{ftype}/{date}.mp4'),
            'filename': f'{date}_{band}.mp4',
            'size': 1000, 'size_gb': 0.001,
        }

    def test_groups_by_date_band(self):
        rows = [
            self._row('2026-01-01', 'Band', 'quadrant'),
            self._row('2026-01-01', 'Band', 'quadrant'),
            self._row('2026-01-02', 'Band', 'raw video', 'source'),
        ]
        states = build_performance_states(rows)
        assert ('2026-01-01', 'Band') in states
        assert ('2026-01-02', 'Band') in states

    def test_routes_cloud_quadrant(self):
        rows = [
            self._row('2026-01-01', 'Band', 'quadrant', location='cloud',
                      path='/c/Users/testuser/OneDrive - No Fun Troy LLC/x.mp4'),
        ]
        states = build_performance_states(rows)
        ps = states[('2026-01-01', 'Band')]
        assert len(ps.cloud_files) == 1
        assert len(ps.quad_files) == 0

    def test_routes_source_wav(self):
        rows = [self._row('2026-01-01', 'Band', 'audio', 'source')]
        states = build_performance_states(rows)
        ps = states[('2026-01-01', 'Band')]
        assert len(ps.raw_wavs) == 1


class TestFilesForPerf:
    """files_for_perf matches by normalised perf identity, not literal prefix —
    so quads encoded under a non-canonical band spelling are still found."""

    def _touch(self, d: pathlib.Path, name: str) -> pathlib.Path:
        p = d / name
        p.write_bytes(b'x')
        return p

    def test_exact_canonical_name_matches(self, tmp_path):
        self._touch(tmp_path, '26-05-13_B_hvpie_CAM1.mp4')
        out = files_for_perf(tmp_path, '_CAM1.mp4', '26-05-13_B_hvpie')
        assert [p.name for p in out] == ['26-05-13_B_hvpie_CAM1.mp4']

    def test_space_and_session_suffix_matches(self, tmp_path):
        # The bug: file encoded as 'B hvpie.25' (space + session) but the perf
        # key normalises to 'B_hvpie'. A literal '{perf}*' glob misses it.
        f = self._touch(tmp_path, '26-05-13_B hvpie.25_CAM1.mp4')
        out = files_for_perf(tmp_path, '_CAM1.mp4', '26-05-13_B_hvpie')
        assert out == [f]

    def test_instagram_output_with_space_name_is_detected(self, tmp_path):
        # The reconciler's reel_ok check must also see the space-named reel.
        self._touch(tmp_path, '26-05-13_B hvpie.25_INSTAGRAM.mp4')
        out = files_for_perf(tmp_path, '_INSTAGRAM.mp4', '26-05-13_B_hvpie')
        assert len(out) == 1

    def test_other_band_is_excluded(self, tmp_path):
        self._touch(tmp_path, '26-05-13_B hvpie.25_CAM1.mp4')
        self._touch(tmp_path, '26-05-13_Grozer_CAM1.mp4')
        out = files_for_perf(tmp_path, '_CAM1.mp4', '26-05-13_B_hvpie')
        assert [p.name for p in out] == ['26-05-13_B hvpie.25_CAM1.mp4']

    def test_suffix_filters_out_other_outputs(self, tmp_path):
        self._touch(tmp_path, '26-05-13_B_hvpie_CAM1.mp4')
        self._touch(tmp_path, '26-05-13_B_hvpie_INSTAGRAM.mp4')
        out = files_for_perf(tmp_path, '_CAM1.mp4', '26-05-13_B_hvpie')
        assert [p.name for p in out] == ['26-05-13_B_hvpie_CAM1.mp4']

    def test_multiple_sessions_returned_sorted(self, tmp_path):
        self._touch(tmp_path, '26-05-13_B hvpie.25_CAM1.mp4')
        self._touch(tmp_path, '26-05-13_B hvpie.10_CAM1.mp4')
        out = files_for_perf(tmp_path, '_CAM1.mp4', '26-05-13_B_hvpie')
        assert [p.name for p in out] == [
            '26-05-13_B hvpie.10_CAM1.mp4',
            '26-05-13_B hvpie.25_CAM1.mp4',
        ]

    def test_missing_directory_returns_empty(self, tmp_path):
        assert files_for_perf(tmp_path / 'nope', '_CAM1.mp4', '26-05-13_B_hvpie') == []
