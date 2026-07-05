from tests.fake_pipeline import FakePipeline


def test_fake_pipeline_storage_matches_explicit_dests(tmp_path):
    fp = FakePipeline(tmp_path)
    assert fp.storage.media_dests(fp.media_root) == {
        'vids_dest': fp.vids_dest,
        'audio_dest': fp.audio_dest,
        'video_archive': fp.video_archive,
        'audio_archive': fp.audio_archive,
    }
