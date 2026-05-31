"""Unit tests for nofun/encoding_db.py."""

import json
import pathlib

import pytest

from nofun.encoding_db import EncodingDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path: pathlib.Path) -> EncodingDB:
    return EncodingDB(tmp_path / 'encoding_db.json')


def _rec(path: str, mtime: float = 1.0, **extra) -> dict:
    return {'path': path, 'mtime': mtime, 'scanned': '2026-03-31T10:00:00', **extra}


# ---------------------------------------------------------------------------
# TestEncodingDBUpsert
# ---------------------------------------------------------------------------

class TestEncodingDBUpsert:
    def test_inserts_new_record(self, db: EncodingDB) -> None:
        db.upsert('2026-03-20', 'OTOBO', 'quadrant_video', _rec('/videos/foo_UL.mp4'))
        perf = db.get_performance('2026-03-20', 'OTOBO')
        assert perf is not None
        assert len(perf['quadrant_video']) == 1

    def test_replaces_existing_same_path(self, db: EncodingDB) -> None:
        db.upsert('2026-03-20', 'OTOBO', 'quadrant_video', _rec('/videos/foo_UL.mp4', codec='h264'))
        db.upsert('2026-03-20', 'OTOBO', 'quadrant_video', _rec('/videos/foo_UL.mp4', codec='hevc'))
        perf = db.get_performance('2026-03-20', 'OTOBO')
        assert len(perf['quadrant_video']) == 1
        assert perf['quadrant_video'][0]['codec'] == 'hevc'

    def test_appends_different_paths(self, db: EncodingDB) -> None:
        db.upsert('2026-03-20', 'OTOBO', 'quadrant_video', _rec('/videos/foo_UL.mp4'))
        db.upsert('2026-03-20', 'OTOBO', 'quadrant_video', _rec('/videos/foo_UR.mp4'))
        perf = db.get_performance('2026-03-20', 'OTOBO')
        assert len(perf['quadrant_video']) == 2

    def test_separate_dates_dont_collide(self, db: EncodingDB) -> None:
        db.upsert('2026-03-20', 'OTOBO', 'quadrant_video', _rec('/a.mp4'))
        db.upsert('2026-03-21', 'OTOBO', 'quadrant_video', _rec('/b.mp4'))
        assert db.get_performance('2026-03-20', 'OTOBO') is not None
        assert db.get_performance('2026-03-21', 'OTOBO') is not None

    def test_separate_bands_dont_collide(self, db: EncodingDB) -> None:
        db.upsert('2026-03-20', 'OTOBO', 'quadrant_video', _rec('/a.mp4'))
        db.upsert('2026-03-20', 'DaisyChain', 'quadrant_video', _rec('/b.mp4'))
        assert db.get_performance('2026-03-20', 'DaisyChain') is not None


# ---------------------------------------------------------------------------
# TestEncodingDBSave
# ---------------------------------------------------------------------------

class TestEncodingDBSave:
    def test_saves_and_reloads(self, tmp_path: pathlib.Path) -> None:
        db1 = EncodingDB(tmp_path / 'db.json')
        db1.upsert('2026-03-20', 'OTOBO', 'quadrant_video', _rec('/a.mp4', codec='hevc'))
        db1.save()

        db2 = EncodingDB(tmp_path / 'db.json')
        perf = db2.get_performance('2026-03-20', 'OTOBO')
        assert perf is not None
        assert perf['quadrant_video'][0]['codec'] == 'hevc'

    def test_save_writes_updated_timestamp(self, tmp_path: pathlib.Path) -> None:
        db = EncodingDB(tmp_path / 'db.json')
        db.save()
        data = json.loads((tmp_path / 'db.json').read_text())
        assert data['updated'] != ''

    def test_corrupt_file_does_not_raise(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'db.json'
        p.write_text('not valid json')
        db = EncodingDB(p)   # should not raise
        assert db.get_performance('x', 'y') is None


# ---------------------------------------------------------------------------
# TestEncodingDBLookup
# ---------------------------------------------------------------------------

class TestEncodingDBLookup:
    def test_finds_record_by_path(self, db: EncodingDB, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'foo_UL.mp4'
        p.write_bytes(b'\x00')
        db.upsert('2026-03-20', 'OTOBO', 'quadrant_video', _rec(str(p)))
        assert db.lookup(p) is not None

    def test_returns_none_for_unknown_path(self, db: EncodingDB, tmp_path: pathlib.Path) -> None:
        assert db.lookup(tmp_path / 'nonexistent.mp4') is None

    def test_finds_across_categories(self, db: EncodingDB, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'foo.zip'
        p.write_bytes(b'\x00')
        db.upsert('2026-03-20', 'OTOBO', 'zipped_audio', _rec(str(p), channel_count=32))
        rec = db.lookup(p)
        assert rec is not None
        assert rec['channel_count'] == 32


# ---------------------------------------------------------------------------
# TestEncodingDBIsStale
# ---------------------------------------------------------------------------

class TestEncodingDBIsStale:
    def test_not_stale_when_mtime_matches(self, db: EncodingDB, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'f.mp4'
        p.write_bytes(b'\x00')
        mtime = p.stat().st_mtime
        rec = _rec(str(p), mtime=mtime)
        assert not db.is_stale(rec, p)

    def test_stale_when_mtime_differs(self, db: EncodingDB, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'f.mp4'
        p.write_bytes(b'\x00')
        rec = _rec(str(p), mtime=0.0)
        assert db.is_stale(rec, p)

    def test_stale_when_file_missing(self, db: EncodingDB, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'gone.mp4'
        rec = _rec(str(p), mtime=1.0)
        assert db.is_stale(rec, p)


# ---------------------------------------------------------------------------
# TestEncodingDBUnscannedPaths
# ---------------------------------------------------------------------------

class TestEncodingDBUnscannedPaths:
    def test_returns_all_when_db_empty(self, db: EncodingDB, tmp_path: pathlib.Path) -> None:
        paths = [tmp_path / 'a.mp4', tmp_path / 'b.mp4']
        for p in paths:
            p.write_bytes(b'\x00')
        assert db.unscanned_paths(paths) == paths

    def test_excludes_fresh_records(self, db: EncodingDB, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'a.mp4'
        p.write_bytes(b'\x00')
        db.upsert('2026-03-20', 'OTOBO', 'quadrant_video',
                  _rec(str(p), mtime=p.stat().st_mtime))
        result = db.unscanned_paths([p])
        assert result == []

    def test_includes_stale_records(self, db: EncodingDB, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'a.mp4'
        p.write_bytes(b'\x00')
        db.upsert('2026-03-20', 'OTOBO', 'quadrant_video',
                  _rec(str(p), mtime=0.0))   # wrong mtime → stale
        result = db.unscanned_paths([p])
        assert result == [p]


# ---------------------------------------------------------------------------
# TestEncodingDBAllPerformances
# ---------------------------------------------------------------------------

class TestEncodingDBPruneOrphanedBands:
    def test_removes_phantom_band(self, db: EncodingDB) -> None:
        db.upsert('26-04-07', 'MX_LONELY', 'quadrant_video', _rec('/a_UL.mp4'))
        db.upsert('26-04-07', 'MX_LONELY_FULLSET', 'source_audio', _rec('/a_FULLSET.wav'))
        pruned = db.prune_orphaned_bands({'26-04-07': {'MX_LONELY'}})
        assert pruned == 1
        assert db.get_performance('26-04-07', 'MX_LONELY') is not None
        assert db.get_performance('26-04-07', 'MX_LONELY_FULLSET') is None

    def test_leaves_unscanned_dates_intact(self, db: EncodingDB) -> None:
        db.upsert('2026-03-01', 'OTOBO', 'quadrant_video', _rec('/a.mp4'))
        # 2026-03-01 not in valid_by_date (drive not mounted during scan)
        pruned = db.prune_orphaned_bands({'2026-04-07': {'MX_LONELY'}})
        assert pruned == 0
        assert db.get_performance('2026-03-01', 'OTOBO') is not None

    def test_returns_zero_when_nothing_stale(self, db: EncodingDB) -> None:
        db.upsert('2026-04-07', 'MX_LONELY', 'quadrant_video', _rec('/a.mp4'))
        assert db.prune_orphaned_bands({'2026-04-07': {'MX_LONELY'}}) == 0

    def test_index_updated_after_prune(self, db: EncodingDB) -> None:
        path = '/26-04-07_MX_LONELY_FULLSET.wav'
        db.upsert('26-04-07', 'MX_LONELY_FULLSET', 'source_audio', _rec(path))
        db.prune_orphaned_bands({'26-04-07': {'MX_LONELY'}})
        assert db.lookup(pathlib.Path(path)) is None

    def test_prunes_multiple_phantoms(self, db: EncodingDB) -> None:
        db.upsert('26-04-07', 'MX_LONELY', 'quadrant_video', _rec('/a.mp4'))
        db.upsert('26-04-07', 'MX_LONELY_FULLSET', 'source_audio', _rec('/b.wav'))
        db.upsert('26-04-07', 'PFC_PRIZE_reel', 'source_audio', _rec('/c.wav'))
        pruned = db.prune_orphaned_bands({'26-04-07': {'MX_LONELY', 'PFC_PRIZE'}})
        assert pruned == 2
        assert db.get_performance('26-04-07', 'MX_LONELY') is not None


class TestEncodingDBAllPerformances:
    def test_sorted_newest_first(self, db: EncodingDB) -> None:
        db.upsert('2026-03-01', 'A', 'quadrant_video', _rec('/a.mp4'))
        db.upsert('2026-03-20', 'B', 'quadrant_video', _rec('/b.mp4'))
        db.upsert('2026-03-10', 'C', 'quadrant_video', _rec('/c.mp4'))
        dates = [d for d, _, _ in db.all_performances()]
        # upsert normalises long YYYY-MM-DD → short YY-MM-DD (§3a)
        assert dates == ['26-03-20', '26-03-10', '26-03-01']

    def test_empty_db_returns_empty(self, db: EncodingDB) -> None:
        assert db.all_performances() == []


# ---------------------------------------------------------------------------
# TestClipsSummary (schema 2)
# ---------------------------------------------------------------------------

class TestClipsSummary:
    def test_set_and_get(self, db: EncodingDB) -> None:
        db.set_clips_summary('2026-03-20', 'BAND', {'dir': '/clips/x', 'count': 5})
        cs = db.get_clips_summary('2026-03-20', 'BAND')
        assert cs is not None
        assert cs['count'] == 5

    def test_get_missing_returns_none(self, db: EncodingDB) -> None:
        assert db.get_clips_summary('9999-01-01', 'NOBODY') is None

    def test_set_replaces_existing(self, db: EncodingDB) -> None:
        db.set_clips_summary('2026-03-20', 'BAND', {'dir': '/clips/x', 'count': 3})
        db.set_clips_summary('2026-03-20', 'BAND', {'dir': '/clips/x', 'count': 7})
        assert db.get_clips_summary('2026-03-20', 'BAND')['count'] == 7


class TestRuntimeSeconds:
    def test_set_writes_band_level_key(self, db: EncodingDB) -> None:
        db.set_runtime_seconds('2026-03-20', 'BAND', 1234.56)
        perf = db.get_performance('2026-03-20', 'BAND')
        assert perf is not None
        assert perf['runtime_seconds'] == 1234.6

    def test_set_is_idempotent_last_write_wins(self, db: EncodingDB) -> None:
        db.set_runtime_seconds('2026-03-20', 'BAND', 100.0)
        db.set_runtime_seconds('2026-03-20', 'BAND', 200.0)
        assert db.get_performance('2026-03-20', 'BAND')['runtime_seconds'] == 200.0

    def test_derive_returns_max_quad_duration(self) -> None:
        perf = {'quadrant_video': [
            {'duration': 1000.0}, {'duration': 1000.1},
            {'duration': 1000.0}, {'duration': 1000.0},
        ]}
        assert EncodingDB.derive_runtime_seconds(perf) == 1000.1

    def test_derive_returns_zero_for_missing_quads(self) -> None:
        assert EncodingDB.derive_runtime_seconds({}) == 0.0
        assert EncodingDB.derive_runtime_seconds({'quadrant_video': []}) == 0.0

    def test_derive_ignores_zero_or_missing_duration(self) -> None:
        perf = {'quadrant_video': [
            {'duration': 0.0}, {}, {'duration': 800.0},
        ]}
        assert EncodingDB.derive_runtime_seconds(perf) == 800.0


class TestSummaryTotalRuntimeSeconds:
    def test_set_summary_persists_total_runtime(self, db: EncodingDB) -> None:
        db.set_summary(5, {'.mp4': 20}, total_runtime_seconds=12345.0)
        s = db.get_summary()
        assert s['total_runtime_seconds'] == 12345.0
        assert s['perf_count'] == 5

    def test_set_summary_defaults_to_zero_when_omitted(self, db: EncodingDB) -> None:
        db.set_summary(3, {'.mp4': 12})
        s = db.get_summary()
        assert s['total_runtime_seconds'] == 0.0

    def test_summary_round_trips_through_save_load(
        self, tmp_path: pathlib.Path,
    ) -> None:
        path = tmp_path / 'enc.json'
        db1 = EncodingDB(path)
        db1.set_summary(7, {'.zip': 4}, total_runtime_seconds=999.9)
        db1.save()
        db2 = EncodingDB(path)
        assert db2.get_summary()['total_runtime_seconds'] == 999.9


class TestMigrateClipsToSummary:
    def test_converts_clips_list(self, db: EncodingDB) -> None:
        db._data = {
            'schema': 1, 'performances': {
                '2026-03-20': {'BAND': {'clips': [
                    {'path': 'D:/clips/26-03-20_BAND/c1.mp4', 'size': 1000,
                     'codec': 'h264', 'resolution': '320x180', 'fps': 30.0,
                     'bitrate_kbps': 200, 'duration': 40.0,
                     'mtime': 1700000000.0, 'scanned': '2026-03-20T00:00:00'},
                    {'path': 'D:/clips/26-03-20_BAND/c2.mp4', 'size': 1100,
                     'codec': 'h264', 'resolution': '320x180', 'fps': 30.0,
                     'bitrate_kbps': 220, 'duration': 40.0,
                     'mtime': 1700000100.0, 'scanned': '2026-03-20T00:00:00'},
                ]}}
            }
        }
        n = db.migrate_clips_to_summary()
        assert n == 1
        perf = db._data['performances']['2026-03-20']['BAND']
        assert 'clips' not in perf
        assert perf['clips_summary']['count'] == 2
        assert perf['clips_summary']['total_size'] == 2100
        assert perf['clips_summary']['codec'] == 'h264'
        assert perf['clips_summary']['dir'] == 'D:/clips/26-03-20_BAND'

    def test_idempotent_skips_existing_summary(self, db: EncodingDB) -> None:
        db._data = {
            'schema': 2, 'performances': {
                '2026-03-20': {'BAND': {'clips_summary': {'dir': '/x', 'count': 3}}}
            }
        }
        n = db.migrate_clips_to_summary()
        assert n == 0

    def test_empty_clips_list_skipped(self, db: EncodingDB) -> None:
        db._data = {
            'schema': 1, 'performances': {
                '2026-03-20': {'BAND': {'clips': []}}
            }
        }
        n = db.migrate_clips_to_summary()
        assert n == 0
        perf = db._data['performances']['2026-03-20']['BAND']
        assert 'clips' not in perf
        assert 'clips_summary' not in perf

    def test_auto_migration_on_load(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'db.json'
        p.write_text(json.dumps({
            'schema': 1,
            'performances': {
                '26-03-20': {'BAND': {'clips': [
                    {'path': 'D:/clips/26-03-20_BAND/c1.mp4', 'size': 500,
                     'mtime': 1700000000.0, 'scanned': '2026-03-20T00:00:00'},
                ]}}
            }
        }))
        db = EncodingDB(p)
        assert db._data['schema'] == 4
        cs = db.get_clips_summary('26-03-20', 'BAND')
        assert cs is not None
        assert cs['count'] == 1


class TestRenameBandClipsSummary:
    def test_renames_clips_summary_dir(self, db: EncodingDB) -> None:
        db.set_clips_summary('2026-03-20', 'OldBand', {
            'dir': 'D:/clips/26-03-20_OldBand', 'count': 2
        })
        db.rename_band('2026-03-20', 'OldBand', 'NewBand')
        assert db.get_clips_summary('2026-03-20', 'OldBand') is None
        cs = db.get_clips_summary('2026-03-20', 'NewBand')
        assert cs is not None
        assert 'NewBand' in cs['dir']
        assert 'OldBand' not in cs['dir']


class TestMigrateNormalizeBandKeys:
    def test_renames_band_key_with_space(self, db: EncodingDB) -> None:
        db._data = {
            'schema': 2, 'performances': {
                '2026-05-13': {'B hvpie': {'some': 'data'}}
            }
        }
        n = db.migrate_normalize_band_keys()
        assert n == 1
        bands = db._data['performances']['2026-05-13']
        assert 'B hvpie' not in bands
        assert 'B_hvpie' in bands

    def test_idempotent_no_spaces(self, db: EncodingDB) -> None:
        db._data = {
            'schema': 3, 'performances': {
                '2026-05-13': {'MALL_GOTH': {'some': 'data'}}
            }
        }
        n = db.migrate_normalize_band_keys()
        assert n == 0

    def test_merge_when_underscored_key_already_exists(self, db: EncodingDB) -> None:
        db._data = {
            'schema': 2, 'performances': {
                '2026-05-13': {
                    'B hvpie': {'old': True},
                    'B_hvpie': {'new': True},
                }
            }
        }
        n = db.migrate_normalize_band_keys()
        assert n == 1
        bands = db._data['performances']['2026-05-13']
        assert 'B hvpie' not in bands
        assert 'B_hvpie' in bands

    def test_auto_migration_on_load(self, tmp_path: pathlib.Path) -> None:
        import json
        p = tmp_path / 'db2.json'
        p.write_text(json.dumps({
            'schema': 2, 'performances': {
                '2026-05-13': {'B hvpie': {'audio': []}}
            }
        }))
        db = EncodingDB(p)
        assert db._data['schema'] == 4
        # the schema-4 date migration also re-keys 2026-05-13 -> 26-05-13
        assert 'B hvpie' not in db._data['performances']['26-05-13']
        assert 'B_hvpie' in db._data['performances']['26-05-13']


class TestMigrateNormalizeDateKeys:
    def test_long_only_key_renamed_records_intact(self, db: EncodingDB) -> None:
        db._data = {
            'schema': 3, 'performances': {
                '2024-12-15': {'OLDSHOW': {
                    'quadrant_video': [{'path': 'D:/v/24-12-15_OLDSHOW/q.mp4'}],
                    'runtime_seconds': 100.0,
                }}
            }
        }
        n = db.migrate_normalize_date_keys()
        assert n == 1
        perfs = db._data['performances']
        assert '2024-12-15' not in perfs
        assert '24-12-15' in perfs
        old = perfs['24-12-15']['OLDSHOW']
        assert old['runtime_seconds'] == 100.0
        assert old['quadrant_video'][0]['path'] == 'D:/v/24-12-15_OLDSHOW/q.mp4'

    def test_twin_keeps_short_records_drops_stale_carries_runtime(self, db: EncodingDB) -> None:
        db._data = {
            'schema': 3, 'performances': {
                '2026-05-25': {  # long twin: stale file records + the only runtime
                    'LIP_CRITIC': {
                        'raw_video': [{'path': 'D:/raw/26-05-25_LIP_CRITIC.mov'}],
                        'runtime_seconds': 222.0,
                    },
                    'LONGONLYBAND': {'quadrant_video': [{'path': 'D:/v/x.mp4'}]},
                },
                '26-05-25': {  # short twin: current-disk truth, no runtime
                    'LIP_CRITIC': {
                        'quadrant_video': [{'path': 'D:/v/26-05-25_LIP_CRITIC/q.mp4'}],
                    },
                },
            }
        }
        n = db.migrate_normalize_date_keys()
        assert n == 1
        perfs = db._data['performances']
        assert '2026-05-25' not in perfs
        short = perfs['26-05-25']
        # short's file-backed records preserved
        assert short['LIP_CRITIC']['quadrant_video'][0]['path'].endswith('q.mp4')
        # long's stale raw_video is NOT unioned back in
        assert 'raw_video' not in short['LIP_CRITIC']
        # runtime_seconds carried over from long into short (the one unrebuildable scalar)
        assert short['LIP_CRITIC']['runtime_seconds'] == 222.0
        # band present only under the long key moved wholesale
        assert 'LONGONLYBAND' in short

    def test_twin_does_not_overwrite_existing_short_runtime(self, db: EncodingDB) -> None:
        db._data = {
            'schema': 3, 'performances': {
                '2026-05-25': {'B': {'runtime_seconds': 111.0}},
                '26-05-25': {'B': {'runtime_seconds': 999.0,
                                   'quadrant_video': [{'path': 'D:/v/q.mp4'}]}},
            }
        }
        db.migrate_normalize_date_keys()
        assert db._data['performances']['26-05-25']['B']['runtime_seconds'] == 999.0

    def test_no_band_dropped_invariant(self, db: EncodingDB) -> None:
        db._data = {
            'schema': 3, 'performances': {
                '2026-05-25': {'A': {}, 'B': {}, 'LONGONLY': {}},
                '26-05-25': {'A': {}, 'C': {}},
                '2024-01-01': {'SOLO': {}},
            }
        }
        # bands reachable before, keyed by normalised date
        before: dict[str, set[str]] = {}
        for d, bands in db._data['performances'].items():
            before.setdefault(d[2:] if len(d) == 10 else d, set()).update(bands)
        db.migrate_normalize_date_keys()
        after = {d: set(bands) for d, bands in db._data['performances'].items()}
        for d, bands in before.items():
            assert bands <= after[d], f"band dropped under {d}"

    def test_idempotent_second_pass_is_noop(self, db: EncodingDB) -> None:
        db._data = {
            'schema': 3, 'performances': {
                '2026-05-25': {'B': {'runtime_seconds': 1.0}},
                '26-05-25': {'B': {'quadrant_video': [{'path': 'D:/v/q.mp4'}]}},
            }
        }
        assert db.migrate_normalize_date_keys() == 1
        assert db.migrate_normalize_date_keys() == 0
        assert [k for k in db._data['performances'] if len(k) == 10] == []

    def test_auto_migration_on_load_bumps_schema_and_backs_up(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'db.json'
        p.write_text(json.dumps({
            'schema': 3, 'performances': {
                '2026-05-25': {'LIP_CRITIC': {'runtime_seconds': 222.0}},
                '26-05-25': {'LIP_CRITIC': {
                    'quadrant_video': [{'path': 'D:/v/26-05-25_LIP_CRITIC/q.mp4'}]}},
            }
        }))
        db = EncodingDB(p)
        assert db._data['schema'] == 4
        perfs = db._data['performances']
        assert '2026-05-25' not in perfs
        assert perfs['26-05-25']['LIP_CRITIC']['runtime_seconds'] == 222.0
        # the migration wrote a pre-mutation backup
        assert (tmp_path / 'db.pre-v4.bak').exists()
        # reloading is a no-op: no long keys remain, schema already 4
        db2 = EncodingDB(p)
        assert db2._data['schema'] == 4
        assert [k for k in db2._data['performances'] if len(k) == 10] == []
