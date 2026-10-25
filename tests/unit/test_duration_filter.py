"""Unit tests for YouTube video duration filtering rules."""

from services.duration_filter import (
    evaluate_duration,
    format_duration,
    parse_iso_duration,
)


class TestDurationFilter:
    """Tests for exact duration boundaries: 3:00 <= duration < 35:00 (180s <= sec < 2100s)."""

    def test_exact_minimum_boundary_180s_is_eligible(self):
        """180 seconds (3:00) is eligible (inclusive minimum)."""
        res = evaluate_duration(180, live_status="none")
        assert res.is_eligible is True
        assert res.skip_reason is None
        assert res.duration_seconds == 180
        assert res.duration_formatted == "3:00"

    def test_just_under_minimum_179s_is_skipped(self):
        """179 seconds (2:59) is skipped as TOO_SHORT."""
        res = evaluate_duration(179, live_status="none")
        assert res.is_eligible is False
        assert res.skip_reason == "TOO_SHORT"
        assert res.duration_formatted == "2:59"

    def test_mid_range_video_is_eligible(self):
        """708 seconds (11:48) is eligible."""
        res = evaluate_duration(708, live_status="none")
        assert res.is_eligible is True
        assert res.skip_reason is None
        assert res.duration_formatted == "11:48"

    def test_just_under_maximum_2099s_is_eligible(self):
        """2099 seconds (34:59) is eligible."""
        res = evaluate_duration(2099, live_status="none")
        assert res.is_eligible is True
        assert res.skip_reason is None
        assert res.duration_formatted == "34:59"

    def test_exact_maximum_boundary_2100s_is_skipped(self):
        """2100 seconds (35:00) is skipped as TOO_LONG (exclusive upper bound)."""
        res = evaluate_duration(2100, live_status="none")
        assert res.is_eligible is False
        assert res.skip_reason == "TOO_LONG"
        assert res.duration_formatted == "35:00"

    def test_over_maximum_3600s_is_skipped(self):
        """3600 seconds (1 hour) is skipped as TOO_LONG."""
        res = evaluate_duration(3600, live_status="none")
        assert res.is_eligible is False
        assert res.skip_reason == "TOO_LONG"
        assert res.duration_formatted == "1:00:00"

    def test_live_stream_is_skipped(self):
        """Videos marked as live or upcoming broadcast are skipped as LIVE_STREAM."""
        res = evaluate_duration(600, live_status="live")
        assert res.is_eligible is False
        assert res.skip_reason == "LIVE_STREAM"

        res_upcoming = evaluate_duration(600, live_status="upcoming")
        assert res_upcoming.is_eligible is False
        assert res_upcoming.skip_reason == "LIVE_STREAM"

    def test_none_or_zero_duration_is_skipped(self):
        """0 or None duration is skipped as INVALID_DURATION."""
        res_zero = evaluate_duration(0, live_status="none")
        assert res_zero.is_eligible is False
        assert res_zero.skip_reason == "INVALID_DURATION"

        res_none = evaluate_duration(None, live_status="none")
        assert res_none.is_eligible is False
        assert res_none.skip_reason == "INVALID_DURATION"


class TestDurationHelpers:
    """Tests for ISO parsing and formatting."""

    def test_parse_iso_duration(self):
        assert parse_iso_duration("PT11M48S") == 708
        assert parse_iso_duration("PT9M35S") == 575
        assert parse_iso_duration("PT3M") == 180
        assert parse_iso_duration("PT30M") == 1800
        assert parse_iso_duration("PT1H2M3S") == 3723
        assert parse_iso_duration("PT0S") == 0
        assert parse_iso_duration("") == 0
        assert parse_iso_duration("INVALID") == 0

    def test_format_duration(self):
        assert format_duration(708) == "11:48"
        assert format_duration(575) == "9:35"
        assert format_duration(180) == "3:00"
        assert format_duration(1800) == "30:00"
        assert format_duration(3723) == "1:02:03"
        assert format_duration(0) == "0:00"
