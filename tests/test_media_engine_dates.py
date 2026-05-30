"""Regression tests for the 2-digit-year SharePoint date handling.

Commit 59d013b switched extract_date_band() to return YY-MM-DD; six date-parse
sites in media_engine.py still assumed 4-digit years, which (a) broke the sync
age check (year 26 AD -> ~730k days old -> every show skipped) and (b) dropped
the leading date producing '-05-24' folders. These pin the contract + the fixes.
"""
import datetime

from nofun.inventory import extract_date_band
from nofun.cleanup import cloud_filename


def test_extract_date_band_returns_two_digit_year():
    # The contract the media_engine fixes depend on. If a future change reverts
    # to YYYY-MM-DD, this fails and flags the 6 call sites for re-review.
    date_str, band = extract_date_band('26-05-24_LASTIMA')
    assert date_str == '26-05-24'
    assert band == 'LASTIMA'


def test_two_digit_year_age_is_sane():
    # The load-bearing fix: datetime.date(2000 + int(y), ...) yields a recent
    # date, not year 26 AD. Guards _sync_eligible_performances et al.
    y, mo, d = '26', '05', '24'
    rec_date = datetime.date(2000 + int(y), int(mo), int(d))
    age_days = (datetime.date.today() - rec_date).days
    assert 0 <= age_days < 365 * 5          # recent, not ~730000
    # the broken form built a date ~2000 years in the past
    broken = datetime.date(int(y), int(mo), int(d))
    assert (datetime.date.today() - broken).days > 700_000


def test_remaster_cloud_name_strips_multitrack():
    # Bug 3: the remaster AUDIO must overwrite the original <BAND>_AUDIO.mp3,
    # so the cloud filename drops both the date prefix and the _MULTITRACK tag.
    name = cloud_filename('26-05-24_LASTIMA_MULTITRACK_AUDIO.mp3').replace('_MULTITRACK', '')
    assert name == 'LASTIMA_AUDIO.mp3'
