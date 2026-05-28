"""Tests for nofun.mastering_meta (Tier 1 mastering metadata)."""
import json

from nofun import mastering_meta as m


def test_should_write_metadata_gates_trials():
    assert m.should_write_metadata(True, None) is True
    assert m.should_write_metadata(True, (60.0, 90.0)) is False   # trial/clip
    assert m.should_write_metadata(False, None) is False          # --all, not AUDIO


def test_channel_stats_crest_dead_clip():
    levels = {29: (-30.0, -8.0), 30: (-91.0, -90.0), 31: (-20.0, -0.05)}
    out = m.channel_stats(levels, align_ms={29: 97.9})
    assert out['29']['crest_db'] == 22.0
    assert out['29']['align_ms'] == 97.9
    assert out['29']['dead'] is False
    assert out['30']['dead'] is True            # -91 < -80
    assert out['31']['clip'] is True            # -0.05 >= -0.1


def test_derive_flags():
    chans = {'29': {'dead': False, 'clip': False}, '30': {'dead': True, 'clip': False},
             '31': {'dead': False, 'clip': True}}
    flags = m.derive_flags(chans, {'rms_delta_db': 13.0}, [(2716.0, 9.1)])
    assert 'dead_channel:30' in flags
    assert 'clip:31' in flags
    assert any(f.startswith('room_board_imbalance') for f in flags)
    assert 'feedback:2716Hz' in flags


def test_derive_flags_clean():
    chans = {'29': {'dead': False, 'clip': False}}
    assert m.derive_flags(chans, {'rms_delta_db': 2.0}, []) == []


def test_build_metadata_keys():
    meta = m.build_metadata('26-05-25_Bejavlvin', {'ch_depth': 70}, {'29': {}},
                            {'rms_delta_db': 5.0}, [(2716.0, 9.1)], ['feedback:2716Hz'])
    for k in ('schema_version', 'performance', 'rendered_at', 'pipeline_sha',
              'recipe', 'channels', 'room_board', 'feedback', 'flags'):
        assert k in meta
    assert meta['schema_version'] == m.SCHEMA_VERSION
    assert meta['feedback']['peaks'][0]['freq'] == 2716.0


def test_append_log_one_line_per_call(tmp_path):
    meta = m.build_metadata('show', {}, {'30': {'dead': True}}, {'rms_delta_db': 1.0},
                            [(2716.0, 9.1)], ['dead_channel:30'])
    m.append_log(meta, tmp_path)
    m.append_log(meta, tmp_path)
    lines = (tmp_path / 'mastering_log.jsonl').read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec['performance'] == 'show'
    assert rec['peaks'] == [2716.0]
    assert rec['dead'] == ['30']


def test_write_sidecar(tmp_path):
    meta = m.build_metadata('show', {'ch_depth': 70}, {}, {}, [], [])
    m.write_sidecar(meta, tmp_path, 'show')
    loaded = json.loads((tmp_path / 'show.json').read_text())
    assert loaded['recipe']['ch_depth'] == 70
