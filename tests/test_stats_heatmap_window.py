"""The stats heatmap shows the 53 weeks ending this week, across New Year."""

from datetime import date, timedelta

from src.web_server import _heatmap_from_mapping, _heatmap_window


def test_window_covers_every_day_the_page_draws():
    today = date(2027, 1, 3)
    start, end = _heatmap_window(today)

    # The page's grid starts on the Sunday 52 weeks before this week's Sunday.
    this_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    grid_start = this_sunday - timedelta(weeks=52)
    assert end == today
    assert start < grid_start


def test_listening_days_from_last_year_stay_in_the_heatmap():
    days = {"2025-01-05": 5, "2025-06-01": 30, "2025-12-31": 60.4, "2026-01-02": 90, "2026-02-01": 10}

    rows = _heatmap_from_mapping(days, date(2025, 1, 6), date(2026, 1, 5))

    assert rows == [
        {"date": "2025-06-01", "seconds": 30},
        {"date": "2025-12-31", "seconds": 60},
        {"date": "2026-01-02", "seconds": 90},
    ]
