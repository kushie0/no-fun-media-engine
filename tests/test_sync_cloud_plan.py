"""Tests for nofun.cleanup.plan_cloud_copy — the SharePoint copy decision table."""
from nofun.cleanup import plan_cloud_copy


def test_default_skips_existing_cloud_copy():
    # dest already in cloud, not overwriting → leave it
    assert plan_cloud_copy(True, False, False, overwrite=False) == 'skip'
    assert plan_cloud_copy(True, True, True, overwrite=False) == 'skip'


def test_default_skips_dehydrated_placeholder():
    # only a dehydrated dated copy exists → renaming forces a download, so skip
    assert plan_cloud_copy(False, True, True, overwrite=False) == 'skip'


def test_default_renames_hydrated_dated_copy():
    # hydrated dated copy present, no stripped-name copy yet → rename in place
    assert plan_cloud_copy(False, True, False, overwrite=False) == 'rename'


def test_default_copies_when_folder_empty():
    assert plan_cloud_copy(False, False, False, overwrite=False) == 'copy'


def test_overwrite_replaces_existing_cloud_copy():
    # the one behavior change: dest exists + overwrite → replace in place
    assert plan_cloud_copy(True, False, False, overwrite=True) == 'overwrite'
    assert plan_cloud_copy(True, True, True, overwrite=True) == 'overwrite'


def test_overwrite_does_not_change_the_no_dest_branches():
    # when no stripped-name copy exists yet, overwrite is irrelevant — same as default
    assert plan_cloud_copy(False, True, True, overwrite=True) == 'skip'
    assert plan_cloud_copy(False, True, False, overwrite=True) == 'rename'
    assert plan_cloud_copy(False, False, False, overwrite=True) == 'copy'
