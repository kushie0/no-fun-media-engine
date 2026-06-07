"""The synthetic smoke-test fixture (band SMOKETEST) must never reach OneDrive.

The manifest SYNC QUADS/AUDIO jobs sync unconditionally — old-date/name tricks
can't dodge them, only a band guard can. These pin the guard's contract: the
helper recognises the reserved band, and the real fixture name parses to it.
"""
from media_engine import _is_smoke_band, SMOKE_TEST_BAND
from nofun.inventory import extract_date_band


def test_is_smoke_band_matches_reserved_name():
    assert _is_smoke_band('SMOKETEST')
    assert _is_smoke_band('smoketest')          # case-insensitive
    assert _is_smoke_band('  SMOKETEST  ')       # tolerates whitespace


def test_is_smoke_band_rejects_real_bands():
    assert not _is_smoke_band('ONE_THRU_TEN')
    assert not _is_smoke_band('LASTIMA')
    assert not _is_smoke_band('')


def test_fixture_name_parses_to_smoke_band():
    # The load-bearing link: the guards extract the band from the perf/file
    # stem, so the fixture name must actually yield SMOKE_TEST_BAND.
    _date_str, band = extract_date_band('01-01-01_SMOKETEST')
    assert band == SMOKE_TEST_BAND
    assert _is_smoke_band(band)
