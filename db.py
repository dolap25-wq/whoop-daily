"""SQLite storage for whoop-daily.

Stores one row per day with the flattened metrics used by the CLI, plus the
raw JSON payloads from each Whoop API call so future trend/visualization
work can pull fields that aren't flattened today without a schema change.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT PRIMARY KEY,
    recovery_pct REAL,
    hrv_ms REAL,
    rhr_bpm REAL,
    sleep_performance_pct REAL,
    sleep_duration_hrs REAL,
    strain REAL,
    recovery_raw TEXT,
    sleep_raw TEXT,
    cycle_raw TEXT,
    fetched_at TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def upsert_daily(
    conn: sqlite3.Connection,
    snap,
    fetched_at: str,
    recovery_raw: dict | None = None,
    sleep_raw: dict | None = None,
    cycle_raw: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO daily_metrics (
            date, recovery_pct, hrv_ms, rhr_bpm,
            sleep_performance_pct, sleep_duration_hrs, strain,
            recovery_raw, sleep_raw, cycle_raw, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            recovery_pct=excluded.recovery_pct,
            hrv_ms=excluded.hrv_ms,
            rhr_bpm=excluded.rhr_bpm,
            sleep_performance_pct=excluded.sleep_performance_pct,
            sleep_duration_hrs=excluded.sleep_duration_hrs,
            strain=excluded.strain,
            recovery_raw=excluded.recovery_raw,
            sleep_raw=excluded.sleep_raw,
            cycle_raw=excluded.cycle_raw,
            fetched_at=excluded.fetched_at
        """,
        (
            snap.date,
            snap.recovery_pct,
            snap.hrv_ms,
            snap.rhr_bpm,
            snap.sleep_performance_pct,
            snap.sleep_duration_hrs,
            snap.strain,
            json.dumps(recovery_raw) if recovery_raw is not None else None,
            json.dumps(sleep_raw) if sleep_raw is not None else None,
            json.dumps(cycle_raw) if cycle_raw is not None else None,
            fetched_at,
        ),
    )
    conn.commit()


def fetch_recent(conn: sqlite3.Connection, n: int) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM daily_metrics ORDER BY date DESC LIMIT ?", (n,)
    )
    rows = cur.fetchall()
    return list(reversed(rows))


def fetch_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM daily_metrics ORDER BY date ASC")
    return cur.fetchall()


def is_empty(conn: sqlite3.Connection) -> bool:
    cur = conn.execute("SELECT 1 FROM daily_metrics LIMIT 1")
    return cur.fetchone() is None


def migrate_from_csv(conn: sqlite3.Connection, csv_path: Path) -> int:
    """One-time import of the legacy CSV log into the DB.

    Only runs when the DB has no rows yet, so it's safe to call on every
    startup. Returns the number of rows imported.
    """
    if not csv_path.exists() or not is_empty(conn):
        return 0

    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))

    count = 0
    for r in rows:
        conn.execute(
            """
            INSERT INTO daily_metrics (
                date, recovery_pct, hrv_ms, rhr_bpm,
                sleep_performance_pct, sleep_duration_hrs, strain, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO NOTHING
            """,
            (
                r["date"],
                _to_float(r.get("recovery_pct")),
                _to_float(r.get("hrv_ms")),
                _to_float(r.get("rhr_bpm")),
                _to_float(r.get("sleep_performance_pct")),
                _to_float(r.get("sleep_duration_hrs")),
                _to_float(r.get("strain")),
                r["date"],
            ),
        )
        count += 1
    conn.commit()

    csv_path.rename(csv_path.with_suffix(".csv.bak"))
    return count


def dedup_by_cycle_id(conn: sqlite3.Connection) -> int:
    """Remove rows that share a cycle_id, keeping the earliest-dated row.

    When fetch_today is called on consecutive days before Whoop has scored a
    new cycle, the same cycle_id ends up stored under two different dates.
    This migration keeps the row with the minimum date (the correct one) and
    deletes the later duplicate.  Returns the number of rows deleted.
    """
    rows = conn.execute(
        "SELECT date, cycle_raw FROM daily_metrics WHERE cycle_raw IS NOT NULL"
    ).fetchall()

    cycle_to_dates: dict[int, list[str]] = {}
    for row in rows:
        try:
            cycle_id = json.loads(row["cycle_raw"]).get("id")
        except (json.JSONDecodeError, TypeError):
            continue
        if cycle_id is not None:
            cycle_to_dates.setdefault(cycle_id, []).append(row["date"])

    deleted = 0
    for cycle_id, dates in cycle_to_dates.items():
        if len(dates) < 2:
            continue
        dates.sort()
        for bad_date in dates[1:]:
            conn.execute("DELETE FROM daily_metrics WHERE date = ?", (bad_date,))
            deleted += 1

    if deleted:
        conn.commit()
    return deleted


def _to_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def fetch_strain_recovery_pairs(
    conn: sqlite3.Connection, days: int | None = None
) -> list[tuple[float, float]]:
    """Return (strain_day_N, recovery_day_N+1) for consecutive calendar days only.

    If days is given, only the last `days` logged rows are considered.
    Pairs where either value is NULL, or where rows are not adjacent calendar
    days, are silently skipped.
    """
    from datetime import date as _date

    rows = fetch_recent(conn, days) if days is not None else fetch_all(conn)
    pairs: list[tuple[float, float]] = []
    for i in range(len(rows) - 1):
        curr, nxt = rows[i], rows[i + 1]
        try:
            curr_d = _date.fromisoformat(curr["date"])
            nxt_d = _date.fromisoformat(nxt["date"])
        except (ValueError, TypeError):
            continue
        if (nxt_d - curr_d).days != 1:
            continue
        if curr["strain"] is None or nxt["recovery_pct"] is None:
            continue
        pairs.append((float(curr["strain"]), float(nxt["recovery_pct"])))
    return pairs
