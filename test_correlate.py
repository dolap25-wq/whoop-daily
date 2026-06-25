import sqlite3
from datetime import date, timedelta

import db


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(db.SCHEMA)
    conn.commit()
    return conn


def _insert(conn, day: str, strain=None, recovery_pct=None, sleep_duration_hrs=None):
    conn.execute(
        """INSERT INTO daily_metrics
           (date, strain, recovery_pct, sleep_duration_hrs, fetched_at)
           VALUES (?, ?, ?, ?, ?)""",
        (day, strain, recovery_pct, sleep_duration_hrs, day),
    )
    conn.commit()


# ── db.fetch_strain_recovery_pairs ──────────────────────────────────────────

def test_pairs_consecutive_days():
    conn = _make_db()
    _insert(conn, "2026-01-01", strain=10.0)
    _insert(conn, "2026-01-02", strain=8.0, recovery_pct=75.0)
    _insert(conn, "2026-01-03", strain=12.0, recovery_pct=55.0)
    pairs = db.fetch_strain_recovery_pairs(conn)
    assert pairs == [(10.0, 75.0), (8.0, 55.0)]


def test_pairs_skips_gap():
    conn = _make_db()
    _insert(conn, "2026-01-01", strain=10.0)
    _insert(conn, "2026-01-03", strain=8.0, recovery_pct=75.0)  # gap — no pair
    assert db.fetch_strain_recovery_pairs(conn) == []


def test_pairs_skips_null_strain():
    conn = _make_db()
    _insert(conn, "2026-01-01", strain=None)
    _insert(conn, "2026-01-02", strain=8.0, recovery_pct=75.0)
    assert db.fetch_strain_recovery_pairs(conn) == []


def test_pairs_skips_null_next_recovery():
    conn = _make_db()
    _insert(conn, "2026-01-01", strain=10.0)
    _insert(conn, "2026-01-02", strain=8.0, recovery_pct=None)
    assert db.fetch_strain_recovery_pairs(conn) == []


def test_pairs_days_limit():
    # days=2 → fetch_recent gives last 2 rows: 2026-01-02 and 2026-01-03
    conn = _make_db()
    _insert(conn, "2026-01-01", strain=14.0)
    _insert(conn, "2026-01-02", strain=10.0, recovery_pct=40.0)
    _insert(conn, "2026-01-03", strain=8.0, recovery_pct=75.0)
    pairs = db.fetch_strain_recovery_pairs(conn, days=2)
    assert pairs == [(10.0, 75.0)]


def test_pairs_empty_db():
    assert db.fetch_strain_recovery_pairs(_make_db()) == []


# ── Stats helpers ────────────────────────────────────────────────────────────

import math
from whoop_daily import _pearson, _ttest_r, _sig_stars, _mat3_inv, _ols, _ftest


def test_pearson_perfect_negative():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [5.0, 4.0, 3.0, 2.0, 1.0]
    assert abs(_pearson(xs, ys) - (-1.0)) < 1e-9


def test_pearson_perfect_positive():
    xs = [1.0, 2.0, 3.0]
    ys = [2.0, 4.0, 6.0]
    assert abs(_pearson(xs, ys) - 1.0) < 1e-9


def test_pearson_constant_y_returns_zero():
    # denominator is zero — correlation undefined, function returns 0.0
    assert _pearson([1.0, 2.0, 3.0], [5.0, 5.0, 5.0]) == 0.0


def test_ttest_r_known_value():
    # r=0.6, n=22 → t = 0.6*sqrt(20)/sqrt(0.64) ≈ 3.354, should be significant
    t_stat, p = _ttest_r(0.6, 22)
    assert abs(t_stat - 3.354) < 0.01
    assert p < 0.05


def test_ttest_r_zero():
    t_stat, p = _ttest_r(0.0, 20)
    assert t_stat == 0.0
    assert p == 1.0


def test_sig_stars_thresholds():
    assert _sig_stars(0.0001) == "***"
    assert _sig_stars(0.005)  == "**"
    assert _sig_stars(0.03)   == "*"
    assert _sig_stars(0.05)   == "(ns)"   # boundary: not strictly < 0.05
    assert _sig_stars(0.2)    == "(ns)"


def test_mat3_inv_identity():
    I = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    inv = _mat3_inv(I)
    for i in range(3):
        for j in range(3):
            assert abs(inv[i][j] - I[i][j]) < 1e-9


def test_mat3_inv_roundtrip():
    # Verify m @ inv(m) ≈ I for a non-trivial matrix
    m = [[2.0, 1.0, 0.0], [1.0, 2.0, 1.0], [0.0, 1.0, 2.0]]
    inv = _mat3_inv(m)
    for i in range(3):
        for j in range(3):
            dot = sum(m[i][k] * inv[k][j] for k in range(3))
            expected = 1.0 if i == j else 0.0
            assert abs(dot - expected) < 1e-9


def test_mat3_inv_singular_raises():
    singular = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]
    raised = False
    try:
        _mat3_inv(singular)
    except ValueError:
        raised = True
    assert raised, "expected ValueError for singular matrix"


def test_ols_perfect_fit():
    # y = 3 + 2*x1 + 0*x2 — OLS should recover exact coefficients
    # x2 varies (non-constant) so XtX is non-singular, but its coefficient is 0
    X = [[1.0, float(i), float(i % 3)] for i in range(1, 7)]
    y = [3.0 + 2.0 * i for i in range(1, 7)]
    betas, residuals, r2, XtX_inv = _ols(X, y)
    assert abs(betas[0] - 3.0) < 1e-6        # intercept
    assert abs(betas[1] - 2.0) < 1e-6        # slope on x1
    assert abs(betas[2] - 0.0) < 1e-6        # slope on x2
    assert abs(r2 - 1.0) < 1e-6
    assert all(abs(e) < 1e-6 for e in residuals)


def test_ols_returns_xtx_inv_shape():
    # x2 varies to avoid collinearity with intercept
    X = [[1.0, float(i), float(i % 3) + 0.5] for i in range(1, 7)]
    y = [float(i) for i in range(1, 7)]
    _, _, _, XtX_inv = _ols(X, y)
    assert len(XtX_inv) == 3
    assert all(len(row) == 3 for row in XtX_inv)


def test_ftest_high_r2():
    F, p = _ftest(r_squared=0.9, n=30, k=2)
    assert F > 100
    assert p < 0.001


def test_ftest_zero_r2():
    F, p = _ftest(r_squared=0.0, n=30, k=2)
    assert F == 0.0
    assert p == 1.0


# ── run_correlate smoke tests ─────────────────────────────────────────────────

from unittest.mock import patch
from whoop_daily import run_correlate


def _make_full_db(n: int = 15):
    """In-memory DB with n consecutive days of complete data."""
    conn = _make_db()
    for i in range(n):
        day = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        _insert(
            conn, day,
            strain=8.0 + i * 0.5,
            recovery_pct=80.0 - i * 2.0,
            sleep_duration_hrs=7.5 - i * 0.05,
        )
    return conn


def test_run_correlate_not_enough_data_no_crash():
    conn = _make_db()
    for i in range(5):
        day = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        _insert(conn, day, strain=10.0, recovery_pct=70.0, sleep_duration_hrs=7.5)
    with patch("whoop_daily.open_db", return_value=conn):
        run_correlate(None)


def test_run_correlate_full_data_no_crash():
    conn = _make_full_db(15)
    with patch("whoop_daily.open_db", return_value=conn):
        run_correlate(None)


def test_run_correlate_days_flag_no_crash():
    conn = _make_full_db(15)
    with patch("whoop_daily.open_db", return_value=conn):
        run_correlate(10)
