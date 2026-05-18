"""tests/test_media_io.py — ETA helper functions."""

from nofun.media_io import compute_ffmpeg_eta, format_eta


class TestFormatEta:
    def test_zero_returns_empty(self):
        assert format_eta(0) == ''

    def test_negative_returns_empty(self):
        assert format_eta(-5) == ''

    def test_sub_second_returns_empty(self):
        assert format_eta(0.5) == ''

    def test_seconds_only(self):
        assert format_eta(15) == 'eta 15s'
        assert format_eta(59) == 'eta 59s'

    def test_minutes_zero_pads_seconds(self):
        assert format_eta(60) == 'eta 1m 00s'
        assert format_eta(125) == 'eta 2m 05s'
        assert format_eta(323) == 'eta 5m 23s'

    def test_large_value(self):
        assert format_eta(3661) == 'eta 61m 01s'

    def test_inf_returns_empty(self):
        assert format_eta(float('inf')) == ''


class TestComputeFfmpegEta:
    def test_unknown_duration_returns_empty(self):
        assert compute_ffmpeg_eta('00:01:00', '2.0x', None) == ''
        assert compute_ffmpeg_eta('00:01:00', '2.0x', 0) == ''

    def test_zero_speed_returns_empty(self):
        assert compute_ffmpeg_eta('00:01:00', '0x', 120.0) == ''

    def test_unparseable_speed_returns_empty(self):
        assert compute_ffmpeg_eta('00:01:00', 'N/A', 120.0) == ''

    def test_simple_eta(self):
        # 120s source, 60s done at 2x speed → 30s remaining wall-clock
        assert compute_ffmpeg_eta('00:01:00', '2.0x', 120.0) == 'eta 30s'

    def test_slow_encode_eta(self):
        # 1800s source, 600s done at 0.5x → 2400s remaining = 40m
        assert compute_ffmpeg_eta('00:10:00', '0.5x', 1800.0) == 'eta 40m 00s'

    def test_finishing_returns_empty(self):
        # 120s source, 119.9s done at 1x → 0.1s remaining → no ETA
        assert compute_ffmpeg_eta('00:01:59.9', '1.0x', 120.0) == ''

    def test_unparseable_tc_treated_as_zero(self):
        # bad tc → encoded=0 → remaining = full duration / speed
        assert compute_ffmpeg_eta('bogus', '1.0x', 60.0) == 'eta 1m 00s'

    def test_speed_with_capital_x(self):
        assert compute_ffmpeg_eta('00:00:00', '2.0X', 60.0) == 'eta 30s'
