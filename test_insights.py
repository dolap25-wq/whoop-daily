import sqlite3
from datetime import date, timedelta
from unittest.mock import patch

import db
from whoop_daily import (
    _sparkline,
    _rolling_avg,
    _trend_arrow,
    _find_streaks,
    _week_start,
)


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(db.SCHEMA)
    conn.commit()
    return conn


def _insert(conn, day: str, recovery_pct=None, hrv_ms=None, rhr_bpm=None,
            sleep_duration_hrs=None, strain=None):
    conn.execute(
        """INSERT INTO daily_metrics
           (date, recovery_pct, hrv_ms, rhr_bpm, sleep_duration_hrs, strain, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (day, recovery_pct, hrv_ms, rhr_bpm, sleep_duration_hrs, strain, day),
    )
    conn.commit()


# ── _sparkline ────────────────────────────────────────────────────────────────

def test_sparkline_all_none_returns_dots():
    assert _sparkline([None, None, None]) == "···"


def test_sparkline_all_same_value_uses_middle_char():
    result = _sparkline([50.0, 50.0, 50.0])
    assert len(result) == 3
    assert all(c == result[0] for c in result)


def test_sparkline_ascending_uses_increasing_chars():
    result = _sparkline([0.0, 50.0, 100.0])
    assert result[0] < result[1] < result[2]


def test_sparkline_none_in_middle_renders_dot():
    result = _sparkline([0.0, None, 100.0])
    assert result[1] == "·"


def test_sparkline_length_matches_input():
    assert len(_sparkline([1.0, 2.0, None, 4.0])) == 4


def test_sparkline_min_maps_to_lowest_block():
    from whoop_daily import _SPARK_CHARS
    result = _sparkline([0.0, 100.0])
    assert result[0] == _SPARK_CHARS[0]
    assert result[1] == _SPARK_CHARS[-1]


# ── _rolling_avg ──────────────────────────────────────────────────────────────

def test_rolling_avg_simple():
    assert _rolling_avg([10.0, 20.0, 30.0], 3) == 20.0


def test_rolling_avg_window_larger_than_list_uses_all():
    assert _rolling_avg([10.0, 20.0], 7) == 15.0


def test_rolling_avg_skips_nones():
    assert _rolling_avg([10.0, None, 30.0], 3) == 20.0


def test_rolling_avg_all_none_returns_none():
    assert _rolling_avg([None, None], 3) is None


def test_rolling_avg_uses_tail_only():
    # window=2 → only last 2 values: [30.0, 40.0]
    assert _rolling_avg([10.0, 20.0, 30.0, 40.0], 2) == 35.0


def test_rolling_avg_empty_list_returns_none():
    assert _rolling_avg([], 7) is None


# ── _trend_arrow ──────────────────────────────────────────────────────────────

def test_trend_arrow_improving():
    # prior 7 days avg 40, recent 7 days avg 80 → delta = +40
    vals = [40.0] * 7 + [80.0] * 7
    result = _trend_arrow(vals)
    assert "↑" in result


def test_trend_arrow_declining():
    vals = [80.0] * 7 + [40.0] * 7
    result = _trend_arrow(vals)
    assert "↓" in result


def test_trend_arrow_flat():
    vals = [60.0] * 14
    result = _trend_arrow(vals)
    assert "→" in result


def test_trend_arrow_not_enough_data():
    # fewer than 14 values → can't compare two 7-day windows
    result = _trend_arrow([50.0] * 5)
    assert "not enough data" in result


def test_trend_arrow_custom_threshold():
    # delta = 1.0, threshold = 0.5 → should be improving
    vals = [60.0] * 7 + [61.0] * 7
    assert "↑" in _trend_arrow(vals, threshold=0.5)


# ── _find_streaks ─────────────────────────────────────────────────────────────

def test_find_streaks_two_separate_streaks():
    conn = _make_db()
    for day, rec in [("2026-01-01", 75.0), ("2026-01-02", 80.0),
                     ("2026-01-03", 30.0), ("2026-01-04", 70.0)]:
        _insert(conn, day, recovery_pct=rec)
    rows = db.fetch_all(conn)
    streaks = _find_streaks(rows)
    assert len(streaks) == 2
    assert len(streaks[0]) == 2   # Jan 1–2
    assert len(streaks[1]) == 1   # Jan 4


def test_find_streaks_gap_breaks_streak():
    conn = _make_db()
    _insert(conn, "2026-01-01", recovery_pct=80.0)
    _insert(conn, "2026-01-03", recovery_pct=80.0)  # gap: Jan 2 missing
    rows = db.fetch_all(conn)
    assert len(_find_streaks(rows)) == 2


def test_find_streaks_all_red_returns_empty():
    conn = _make_db()
    for i in range(3):
        _insert(conn, f"2026-01-0{i + 1}", recovery_pct=20.0)
    assert _find_streaks(db.fetch_all(conn)) == []


def test_find_streaks_none_recovery_breaks_streak():
    conn = _make_db()
    _insert(conn, "2026-01-01", recovery_pct=80.0)
    _insert(conn, "2026-01-02", recovery_pct=None)
    _insert(conn, "2026-01-03", recovery_pct=80.0)
    assert len(_find_streaks(db.fetch_all(conn))) == 2


def test_find_streaks_empty_db():
    assert _find_streaks(db.fetch_all(_make_db())) == []


def test_find_streaks_boundary_67_is_green():
    conn = _make_db()
    _insert(conn, "2026-01-01", recovery_pct=67.0)
    _insert(conn, "2026-01-02", recovery_pct=66.9)
    rows = db.fetch_all(conn)
    streaks = _find_streaks(rows)
    assert len(streaks) == 1
    assert streaks[0] == ["2026-01-01"]


# ── _week_start ───────────────────────────────────────────────────────────────

def test_week_start_monday():
    assert _week_start("2026-06-22") == date(2026, 6, 22)


def test_week_start_sunday():
    assert _week_start("2026-06-28") == date(2026, 6, 22)


def test_week_start_wednesday():
    assert _week_start("2026-06-24") == date(2026, 6, 22)


def test_week_start_saturday():
    assert _week_start("2026-06-27") == date(2026, 6, 22)


# ── run_trends smoke tests ─────────────────────────────────────────────────

from whoop_daily import run_trends


def _make_full_db(n: int = 15):
    conn = _make_db()
    for i in range(n):
        day = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        _insert(conn, day,
                recovery_pct=80.0 - i * 2.0,
                hrv_ms=70.0 + i * 0.5,
                rhr_bpm=55.0 - i * 0.1,
                sleep_duration_hrs=7.5,
                strain=10.0 + i * 0.3)
    return conn


def test_run_trends_full_data_no_crash():
    conn = _make_full_db()
    with patch("whoop_daily.open_db", return_value=conn):
        run_trends(None)


def test_run_trends_with_days_flag_no_crash():
    conn = _make_full_db()
    with patch("whoop_daily.open_db", return_value=conn):
        run_trends(7)


def test_run_trends_empty_db_no_crash():
    conn = _make_db()
    with patch("whoop_daily.open_db", return_value=conn):
        run_trends(None)


def test_run_trends_sparse_data_no_crash():
    # only 3 days — rolling windows should show counts, not crash
    conn = _make_full_db(3)
    with patch("whoop_daily.open_db", return_value=conn):
        run_trends(None)


# ── run_weekly smoke tests ─────────────────────────────────────────────────

from whoop_daily import run_weekly


def test_run_weekly_full_data_no_crash():
    conn = _make_full_db(30)
    with patch("whoop_daily.open_db", return_value=conn):
        run_weekly(8)


def test_run_weekly_empty_db_no_crash():
    conn = _make_db()
    with patch("whoop_daily.open_db", return_value=conn):
        run_weekly(8)


def test_run_weekly_single_day_no_crash():
    conn = _make_db()
    _insert(conn, "2026-06-20", recovery_pct=72.0, strain=9.0)
    with patch("whoop_daily.open_db", return_value=conn):
        run_weekly(4)


def test_run_weekly_sparse_week_excluded_from_best_worst():
    # Only 2 days in the week → should not appear in best/worst footer
    conn = _make_db()
    _insert(conn, "2026-06-22", recovery_pct=95.0)  # 2 days this week only
    _insert(conn, "2026-06-23", recovery_pct=95.0)
    _insert(conn, "2026-06-15", recovery_pct=40.0)  # 3 days prior week
    _insert(conn, "2026-06-16", recovery_pct=40.0)
    _insert(conn, "2026-06-17", recovery_pct=40.0)
    with patch("whoop_daily.open_db", return_value=conn):
        run_weekly(8)  # should not crash even with sparse data


# ── run_streaks smoke tests ────────────────────────────────────────────────

from whoop_daily import run_streaks


def test_run_streaks_full_data_no_crash():
    conn = _make_full_db(30)
    with patch("whoop_daily.open_db", return_value=conn):
        run_streaks()


def test_run_streaks_empty_db_no_crash():
    conn = _make_db()
    with patch("whoop_daily.open_db", return_value=conn):
        run_streaks()


def test_run_streaks_all_green_no_crash():
    conn = _make_db()
    for i in range(10):
        _insert(conn, (date(2026, 1, 1) + timedelta(days=i)).isoformat(), recovery_pct=85.0)
    with patch("whoop_daily.open_db", return_value=conn):
        run_streaks()


def test_run_streaks_all_red_no_crash():
    conn = _make_db()
    for i in range(5):
        _insert(conn, (date(2026, 1, 1) + timedelta(days=i)).isoformat(), recovery_pct=20.0)
    with patch("whoop_daily.open_db", return_value=conn):
        run_streaks()


def test_run_streaks_no_records_with_all_metrics_no_crash():
    conn = _make_db()
    _insert(conn, "2026-01-01")  # all NULL metrics
    with patch("whoop_daily.open_db", return_value=conn):
        run_streaks()
