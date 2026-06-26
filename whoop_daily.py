#!/usr/bin/env python3
"""whoop-daily: a tiny CLI for pulling your Whoop recovery/sleep/strain
and keeping a local trend log.

Commands:
    setup    one-time OAuth login, saves tokens locally
    today    pull today's recovery/sleep/strain, print summary, log to CSV
    history  show recent entries from the local log
"""

from __future__ import annotations

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import http.server
import json
import logging
import math
import secrets
import sqlite3
import string
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import db

APP_DIR = Path.home() / ".whoop-daily"
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "log.csv"
DB_PATH = APP_DIR / "whoop.db"
LOG_FILE = APP_DIR / "run.log"

log = logging.getLogger("whoop_daily")


def setup_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer/v2"
SCOPES = "read:recovery read:sleep read:workout read:cycles read:profile offline"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

console = Console(legacy_windows=False)


# ---------------------------------------------------------------------------
# Stats helpers (pure Python except scipy CDFs)
# ---------------------------------------------------------------------------

def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson r. Returns 0.0 when either series is constant (undefined)."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_sq = sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)
    return 0.0 if den_sq == 0 else num / math.sqrt(den_sq)


def _ttest_r(r: float, n: int) -> tuple[float, float]:
    """Two-tailed t-test on Pearson r. Returns (t_stat, p_value)."""
    import scipy.stats
    if r == 0.0:
        return 0.0, 1.0
    if abs(r) >= 1.0:
        return float("inf"), 0.0
    t_stat = r * math.sqrt(n - 2) / math.sqrt(1 - r ** 2)
    p = float(2 * scipy.stats.t.sf(abs(t_stat), df=n - 2))
    return t_stat, p


def _sig_stars(p: float) -> str:
    if p < 0.001:
        return "***"
    if p < 0.01:
        return "**"
    if p < 0.05:
        return "*"
    return "(ns)"


# ---------------------------------------------------------------------------
# Insight helpers (pure functions — no I/O, no DB)
# ---------------------------------------------------------------------------

_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float | None]) -> str:
    """Map a sequence of optional floats to a single-line block-char sparkline.

    None entries render as '·'. When all values are the same, the middle
    block char is used so the line isn't misleadingly flat.
    """
    non_none = [v for v in values if v is not None]
    if not non_none:
        return "·" * len(values)
    lo, hi = min(non_none), max(non_none)
    result = []
    for v in values:
        if v is None:
            result.append("·")
        elif hi == lo:
            result.append(_SPARK_CHARS[4])
        else:
            idx = round((v - lo) / (hi - lo) * (len(_SPARK_CHARS) - 1))
            result.append(_SPARK_CHARS[idx])
    return "".join(result)


def _rolling_avg(values: list[float | None], window: int) -> float | None:
    """Average the last `window` non-None values. Returns None if none exist."""
    tail = [v for v in values[-window:] if v is not None]
    return sum(tail) / len(tail) if tail else None


def _trend_arrow(vals: list[float | None], threshold: float = 2.0) -> str:
    """Compare last 7-day avg to prior 7-day avg and return a Rich-markup string."""
    recent = [v for v in vals[-7:] if v is not None]
    prior = [v for v in vals[-14:-7] if v is not None]
    if not recent or not prior:
        return "[dim]→ (not enough data)[/]"
    delta = sum(recent) / len(recent) - sum(prior) / len(prior)
    if delta > threshold:
        return f"[green]↑ improving[/] (+{delta:.1f})"
    if delta < -threshold:
        return f"[red]↓ declining[/] ({delta:.1f})"
    return f"[yellow]→ flat[/] ({delta:+.1f})"


def _find_streaks(rows: list) -> list[list[str]]:
    """Find all consecutive calendar-day green streaks (recovery_pct >= 67).

    Rows must be sorted ascending by date (as returned by db.fetch_all).
    Returns a list of streaks; each streak is a list of date strings.
    """
    streaks: list[list[str]] = []
    current: list[str] = []
    for row in rows:
        d_str = row["date"]
        rec = row["recovery_pct"]
        is_green = rec is not None and float(rec) >= 67.0
        if is_green:
            if current:
                prev_d = date.fromisoformat(current[-1])
                curr_d = date.fromisoformat(d_str)
                if (curr_d - prev_d).days == 1:
                    current.append(d_str)
                else:
                    streaks.append(current[:])
                    current = [d_str]
            else:
                current = [d_str]
        else:
            if current:
                streaks.append(current[:])
                current = []
    if current:
        streaks.append(current[:])
    return streaks


def _week_start(date_str: str) -> date:
    """Return the Monday that starts the calendar week containing date_str."""
    d = date.fromisoformat(date_str)
    return d - timedelta(days=d.weekday())


def _mat3_inv(m: list[list[float]]) -> list[list[float]]:
    """Invert a 3×3 matrix via cofactor expansion. Raises ValueError if singular."""
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise ValueError("singular matrix — predictors may be perfectly collinear")
    s = 1.0 / det
    return [
        [(e*i - f*h)*s, (c*h - b*i)*s, (b*f - c*e)*s],
        [(f*g - d*i)*s, (a*i - c*g)*s, (c*d - a*f)*s],
        [(d*h - e*g)*s, (b*g - a*h)*s, (a*e - b*d)*s],
    ]


def _ols(
    X: list[list[float]], y: list[float]
) -> tuple[list[float], list[float], float, list[list[float]]]:
    """OLS regression. X rows must be [1.0, x1, x2]. Returns (betas, residuals, R², XtX_inv)."""
    n = len(y)
    k = len(X[0])
    XtX = [[sum(X[r][i] * X[r][j] for r in range(n)) for j in range(k)] for i in range(k)]
    Xty = [sum(X[r][i] * y[r] for r in range(n)) for i in range(k)]
    XtX_inv = _mat3_inv(XtX)
    betas = [sum(XtX_inv[i][j] * Xty[j] for j in range(k)) for i in range(k)]
    y_hat = [sum(betas[j] * X[r][j] for j in range(k)) for r in range(n)]
    residuals = [y[r] - y_hat[r] for r in range(n)]
    y_mean = sum(y) / n
    TSS = sum((v - y_mean) ** 2 for v in y)
    RSS = sum(e ** 2 for e in residuals)
    r2 = 1.0 - RSS / TSS if TSS > 0 else 0.0
    return betas, residuals, r2, XtX_inv


def _ftest(r_squared: float, n: int, k: int) -> tuple[float, float]:
    """Overall F-test for OLS model significance. Returns (F_stat, p_value)."""
    import scipy.stats
    if r_squared <= 0.0:
        return 0.0, 1.0
    if r_squared >= 1.0:
        return float("inf"), 0.0
    F = (r_squared / k) / ((1 - r_squared) / (n - k - 1))
    p = float(scipy.stats.f.sf(F, k, n - k - 1))
    return F, p


# ---------------------------------------------------------------------------
# Config / token storage
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text())


def save_config(config: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass  # chmod is best-effort on Windows


# ---------------------------------------------------------------------------
# OAuth setup
# ---------------------------------------------------------------------------

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    auth_code: str | None = None

    def do_GET(self):  # noqa: N802 (stdlib method name)
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            body = b"<html><body>Got it, you can close this tab.</body></html>"
            self.send_response(200)
        else:
            body = b"<html><body>No auth code received.</body></html>"
            self.send_response(400)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence default request logging
        pass


def run_setup() -> None:
    console.print(Panel.fit("Whoop Daily - one-time setup", style="bold cyan"))

    config = load_config()
    client_id = console.input(
        "Whoop Client ID"
        + (f" [{config.get('client_id')}]" if config.get("client_id") else "")
        + ": "
    ) or config.get("client_id")
    client_secret = console.input(
        "Whoop Client Secret"
        + (" [unchanged]" if config.get("client_secret") else "")
        + ": "
    ) or config.get("client_secret")

    if not client_id or not client_secret:
        console.print(
            "[red]Need a Client ID and Secret.[/] Register an app at "
            "https://developer.whoop.com to get these, then re-run `setup`."
        )
        return

    # WHOOP requires a self-generated state value to be exactly 8 characters.
    state = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))

    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
        }
    )
    auth_url = f"{AUTH_URL}?{query}"

    console.print("\nOpening your browser to log in to Whoop...")
    console.print(f"If it doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    console.print("Waiting for the login redirect...")
    while _CallbackHandler.auth_code is None:
        server.handle_request()
    code = _CallbackHandler.auth_code

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=15,
    )
    resp.raise_for_status()
    tokens = resp.json()

    config.update(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
        }
    )
    save_config(config)
    console.print("[green]Setup complete.[/] Run `python whoop_daily.py today` any time.")


def ensure_token(config: dict) -> str:
    """Return a usable access token, refreshing it if needed."""
    if not config.get("access_token"):
        raise RuntimeError("Not set up yet. Run `python whoop_daily.py setup` first.")

    # Try the current token; if the API says it's expired, refresh and retry once.
    return config["access_token"]


def refresh_token(config: dict) -> str:
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": config["refresh_token"],
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    tokens = resp.json()
    config["access_token"] = tokens["access_token"]
    config["refresh_token"] = tokens.get("refresh_token", config["refresh_token"])
    save_config(config)
    return config["access_token"]


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def api_get(config: dict, path: str, params: dict | None = None) -> dict:
    token = ensure_token(config)
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=15)

    if resp.status_code == 401:
        token = refresh_token(config)
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=15)

    resp.raise_for_status()
    return resp.json()


@dataclass
class DailySnapshot:
    date: str
    recovery_pct: float | None = None
    hrv_ms: float | None = None
    rhr_bpm: float | None = None
    sleep_performance_pct: float | None = None
    sleep_duration_hrs: float | None = None
    strain: float | None = None


def _whoop_local_date(utc_str: str, tz_offset: str) -> str:
    """Convert a Whoop UTC timestamp + timezone_offset string to a local date.

    tz_offset is the value Whoop returns in cycle records, e.g. "-04:00".
    """
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    sign = 1 if tz_offset[0] == "+" else -1
    h, m = int(tz_offset[1:3]), int(tz_offset[4:6])
    local_dt = dt + timedelta(hours=sign * h, minutes=sign * m)
    return local_dt.date().isoformat()


def fetch_today(config: dict) -> tuple[DailySnapshot, dict, dict, dict]:
    recovery = api_get(config, "/recovery", params={"limit": 1})
    recovery_record = None
    records = recovery.get("records", [])
    recovery_score: dict = {}
    if records:
        recovery_record = records[0]
        recovery_score = recovery_record.get("score", {})

    sleep = api_get(config, "/activity/sleep", params={"limit": 1})
    sleep_record = None
    records = sleep.get("records", [])
    sleep_score: dict = {}
    if records:
        sleep_record = records[0]
        sleep_score = sleep_record.get("score", {})

    cycle = api_get(config, "/cycle", params={"limit": 1})
    cycle_record = None
    records = cycle.get("records", [])
    if records:
        cycle_record = records[0]

    # Derive the authoritative local date from recovery.created_at so rows are
    # keyed to the day Whoop scored the recovery (the morning you woke up), not
    # the UTC wall-clock time we happened to call fetch_today.  Without this,
    # running `today` on two consecutive calendar days while Whoop is still
    # returning the same completed cycle produces duplicate rows.
    tz_offset = (cycle_record or {}).get("timezone_offset") or "+00:00"
    if recovery_record and recovery_record.get("created_at"):
        today = _whoop_local_date(recovery_record["created_at"], tz_offset)
    elif cycle_record and cycle_record.get("start"):
        today = _whoop_local_date(cycle_record["start"], tz_offset)
    else:
        today = datetime.now(timezone.utc).date().isoformat()

    snap = DailySnapshot(date=today)
    snap.recovery_pct = recovery_score.get("recovery_score")
    snap.hrv_ms = recovery_score.get("hrv_rmssd_milli")
    snap.rhr_bpm = recovery_score.get("resting_heart_rate")
    snap.sleep_performance_pct = sleep_score.get("sleep_performance_percentage")
    duration_ms = sleep_score.get("stage_summary", {}).get("total_in_bed_time_milli")
    if duration_ms:
        snap.sleep_duration_hrs = round(duration_ms / 1000 / 60 / 60, 2)
    if cycle_record:
        snap.strain = cycle_record.get("score", {}).get("strain")

    return snap, recovery_record, sleep_record, cycle_record


# ---------------------------------------------------------------------------
# correlate helpers
# ---------------------------------------------------------------------------

_CHART_WIDTH = 40

_STRAIN_BUCKETS: list[tuple[str, float, float]] = [
    ("Low",      0.0,  8.0),
    ("Moderate", 8.0,  12.0),
    ("High",     12.0, 16.0),
    ("All-out",  16.0, 21.0),
]

_SLEEP_BUCKETS: list[tuple[str, float, float]] = [
    ("< 6 h", 0.0,  6.0),
    ("6–7 h", 6.0,  7.0),
    ("7–8 h", 7.0,  8.0),
    ("8–9 h", 8.0,  9.0),
    ("9 h+",  9.0, 99.0),
]


def _bar_chart(title: str, buckets: list[tuple[str, list[float]]], label_width: int = 9) -> None:
    """Print a colored horizontal bar chart of average recovery per bucket."""
    console.print(f"\n[bold]{title}[/]\n")
    has_data = False
    for label, values in buckets:
        if not values:
            continue
        has_data = True
        avg = sum(values) / len(values)
        n = len(values)
        bar_len = round(avg / 100 * _CHART_WIDTH)
        style = recovery_style(avg)
        bar = "█" * bar_len
        console.print(
            f"  {label:<{label_width}} "
            f"[{style}]{bar:<{_CHART_WIDTH}}[/] "
            f"[dim]{avg:>5.1f}% (n={n})[/]"
        )
    if not has_data:
        console.print("  [dim](no data in any bucket)[/]")


def _build_ols_triples(
    rows: list,
) -> tuple[list[list[float]], list[float]]:
    """Build (X, y) for OLS from consecutive-day rows.

    X rows: [1.0, strain_day_N, sleep_duration_hrs_day_N+1]
    y:      recovery_pct_day_N+1

    Skips pairs where any of the three values is NULL or the two rows
    are not adjacent calendar days.
    """
    from datetime import date as _date

    X: list[list[float]] = []
    y: list[float] = []
    for i in range(len(rows) - 1):
        curr, nxt = rows[i], rows[i + 1]
        try:
            curr_d = _date.fromisoformat(curr["date"])
            nxt_d = _date.fromisoformat(nxt["date"])
        except (ValueError, TypeError):
            continue
        if (nxt_d - curr_d).days != 1:
            continue
        if any(
            v is None
            for v in [curr["strain"], nxt["sleep_duration_hrs"], nxt["recovery_pct"]]
        ):
            continue
        X.append([1.0, float(curr["strain"]), float(nxt["sleep_duration_hrs"])])
        y.append(float(nxt["recovery_pct"]))
    return X, y


def run_correlate(days: int | None) -> None:
    import scipy.stats

    conn = open_db()
    all_rows = db.fetch_recent(conn, days) if days is not None else db.fetch_all(conn)

    console.print(Panel.fit("Whoop – Correlation Analysis", style="bold cyan"))

    # ── Section 1: Strain → Next-Day Recovery ──────────────────────────────
    strain_pairs = db.fetch_strain_recovery_pairs(conn, days)

    if len(strain_pairs) < 10:
        console.print(
            f"[yellow]Strain → Recovery:[/] need at least 10 consecutive-day pairs "
            f"(have {len(strain_pairs)}). Run `today` more often."
        )
    else:
        strain_xs = [p[0] for p in strain_pairs]
        rec_ys = [p[1] for p in strain_pairs]
        buckets = [
            (label, [rec for s, rec in strain_pairs if lo <= s < hi])
            for label, lo, hi in _STRAIN_BUCKETS
        ]
        _bar_chart("Strain → Next-Day Recovery", buckets, label_width=9)
        r = _pearson(strain_xs, rec_ys)
        t_stat, p = _ttest_r(r, len(strain_pairs))
        p_str = "<0.001" if p < 0.001 else f"{p:.4f}"
        sig = "significant" if p < 0.05 else "not significant"
        console.print(
            f"\n  r = {r:+.3f}   t = {t_stat:+.2f}   "
            f"df = {len(strain_pairs) - 2}   p = {p_str}  "
            f"{_sig_stars(p)} {sig} (α=0.05)"
        )
        console.print("  H₀: ρ = 0 (no linear relationship)   H₁: ρ ≠ 0\n")

    # ── Section 2: Sleep Duration → Recovery ───────────────────────────────
    sleep_pairs = [
        (float(r["sleep_duration_hrs"]), float(r["recovery_pct"]))
        for r in all_rows
        if r["sleep_duration_hrs"] is not None and r["recovery_pct"] is not None
    ]

    if len(sleep_pairs) < 10:
        console.print(
            f"[yellow]Sleep → Recovery:[/] need at least 10 data points "
            f"(have {len(sleep_pairs)}). Run `today` more often."
        )
    else:
        sleep_xs = [p[0] for p in sleep_pairs]
        rec_ys2 = [p[1] for p in sleep_pairs]
        buckets2 = [
            (label, [rec for s, rec in sleep_pairs if lo <= s < hi])
            for label, lo, hi in _SLEEP_BUCKETS
        ]
        _bar_chart("Sleep Duration → Recovery", buckets2, label_width=7)
        r2 = _pearson(sleep_xs, rec_ys2)
        t_stat2, p2 = _ttest_r(r2, len(sleep_pairs))
        p_str2 = "<0.001" if p2 < 0.001 else f"{p2:.4f}"
        sig2 = "significant" if p2 < 0.05 else "not significant"
        console.print(
            f"\n  r = {r2:+.3f}   t = {t_stat2:+.2f}   "
            f"df = {len(sleep_pairs) - 2}   p = {p_str2}  "
            f"{_sig_stars(p2)} {sig2} (α=0.05)"
        )
        console.print("  H₀: ρ = 0 (no linear relationship)   H₁: ρ ≠ 0\n")

    # ── Section 3: Multiple Regression ─────────────────────────────────────
    X_ols, y_ols = _build_ols_triples(all_rows)
    n_ols = len(y_ols)

    if n_ols < 10:
        console.print(
            f"[yellow]Multiple regression:[/] need at least 10 complete triples "
            f"(strain_lag1 + sleep_hrs + recovery, have {n_ols})."
        )
    else:
        try:
            betas, residuals, r_sq, XtX_inv = _ols(X_ols, y_ols)
        except ValueError as exc:
            console.print(f"[red]Cannot compute regression:[/] {exc}")
        else:
            k = 2
            RSS = sum(e ** 2 for e in residuals)
            se2 = RSS / (n_ols - k - 1)
            se_betas = [math.sqrt(XtX_inv[i][i] * se2) for i in range(3)]
            t_betas = [
                betas[i] / se_betas[i] if se_betas[i] > 0 else 0.0
                for i in range(3)
            ]
            p_betas = [
                float(2 * scipy.stats.t.sf(abs(t), df=n_ols - k - 1))
                for t in t_betas
            ]
            F_stat, p_F = _ftest(r_sq, n_ols, k)

            from rich.table import Table as RichTable

            tbl = RichTable(
                title=(
                    f"OLS: recovery ~ strain_lag1 + sleep_duration_hrs"
                    f"    n = {n_ols}"
                ),
                show_header=True,
                header_style="bold",
            )
            tbl.add_column("", style="bold")
            tbl.add_column("coef", justify="right")
            tbl.add_column("std err", justify="right")
            tbl.add_column("t", justify="right")
            tbl.add_column("p", justify="right")
            tbl.add_column("", justify="left")

            names = ["intercept", "strain_lag1", "sleep_hrs"]
            for i, name in enumerate(names):
                p_b_str = "<0.001" if p_betas[i] < 0.001 else f"{p_betas[i]:.3f}"
                tbl.add_row(
                    name,
                    f"{betas[i]:+.2f}",
                    f"{se_betas[i]:.2f}",
                    f"{t_betas[i]:+.2f}",
                    p_b_str,
                    _sig_stars(p_betas[i]),
                )
            console.print(tbl)

            p_F_str = "<0.001" if p_F < 0.001 else f"{p_F:.4f}"
            console.print(
                f"\n  R² = {r_sq:.3f}   F({k}, {n_ols - k - 1}) = {F_stat:.1f}"
                f"   p = {p_F_str}  {_sig_stars(p_F)}"
            )
            console.print(
                "  H₀: β_strain = β_sleep = 0   H₁: at least one β ≠ 0\n"
            )

            # ── Section 4: Multicollinearity ───────────────────────────────────
            r_12 = _pearson(
                [row[1] for row in X_ols],
                [row[2] for row in X_ols],
            )
            vif = 1.0 / (1.0 - r_12 ** 2) if abs(r_12) < 1.0 else float("inf")

            def _vif_label(v: float) -> str:
                if v < 5:
                    return "[green]✓ fine[/]"
                if v < 10:
                    return "[yellow]⚠ moderate[/]"
                return "[red]✗ high[/]"

            console.print(
                Panel(
                    f"Predictor correlation (strain_lag1 vs sleep_hrs):  "
                    f"r = {r_12:+.3f}\n"
                    f"VIF (strain_lag1):    {vif:.2f}   {_vif_label(vif)}\n"
                    f"VIF (sleep_duration): {vif:.2f}   {_vif_label(vif)}\n"
                    "[dim](both predictors share the same VIF with 2-predictor models)[/]\n\n"
                    "[dim]VIF guide: <5 fine · 5–10 moderate · >10 high "
                    "(collinear predictors inflate std errors)[/]",
                    title="Multicollinearity",
                    border_style="blue",
                )
            )


# ---------------------------------------------------------------------------
# Logging / history
# ---------------------------------------------------------------------------

def open_db() -> sqlite3.Connection:
    conn = db.connect(DB_PATH)
    imported = db.migrate_from_csv(conn, LOG_PATH)
    if imported:
        console.print(f"[dim]Migrated {imported} row(s) from {LOG_PATH.name} into {DB_PATH.name}[/]")
    removed = db.dedup_by_cycle_id(conn)
    if removed:
        console.print(f"[dim]Removed {removed} duplicate row(s) (same Whoop cycle stored on multiple dates)[/]")
    return conn


def recovery_style(pct: float | None) -> str:
    if pct is None:
        return "white"
    if pct >= 67:
        return "green"
    if pct >= 34:
        return "yellow"
    return "red"


def print_summary(snap: DailySnapshot) -> None:
    style = recovery_style(snap.recovery_pct)
    lines = [
        f"[bold {style}]Recovery:[/] "
        + (f"{snap.recovery_pct:.0f}%" if snap.recovery_pct is not None else "n/a"),
        f"HRV: {snap.hrv_ms:.0f} ms" if snap.hrv_ms is not None else "HRV: n/a",
        f"RHR: {snap.rhr_bpm:.0f} bpm" if snap.rhr_bpm is not None else "RHR: n/a",
        f"Sleep performance: {snap.sleep_performance_pct:.0f}%"
        if snap.sleep_performance_pct is not None
        else "Sleep performance: n/a",
        f"Sleep duration: {snap.sleep_duration_hrs:.1f} hrs"
        if snap.sleep_duration_hrs is not None
        else "Sleep duration: n/a",
        f"Strain: {snap.strain:.1f}" if snap.strain is not None else "Strain: n/a",
    ]
    console.print(Panel("\n".join(lines), title=f"Whoop - {snap.date}", border_style=style))


def run_today() -> bool:
    config = load_config()
    if not config.get("access_token"):
        console.print(
            "[yellow]Not set up yet.[/] Run `python whoop_daily.py setup` first "
            "(needs a Whoop developer Client ID/Secret from https://developer.whoop.com)."
        )
        log.warning("today: not set up yet")
        return False

    try:
        snap, recovery_raw, sleep_raw, cycle_raw = fetch_today(config)
    except requests.HTTPError as exc:
        console.print(f"[red]Whoop API error:[/] {exc}")
        log.error("today: Whoop API error: %s", exc)
        return False
    except Exception as exc:
        console.print(f"[red]Unexpected error:[/] {exc}")
        log.exception("today: unexpected error")
        return False

    print_summary(snap)
    conn = open_db()
    db.upsert_daily(
        conn,
        snap,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        recovery_raw=recovery_raw,
        sleep_raw=sleep_raw,
        cycle_raw=cycle_raw,
    )
    console.print(f"[dim]Logged to {DB_PATH}[/]")
    log.info(
        "today: ok date=%s recovery=%s hrv=%s rhr=%s sleep_pct=%s sleep_hrs=%s strain=%s",
        snap.date,
        snap.recovery_pct,
        snap.hrv_ms,
        snap.rhr_bpm,
        snap.sleep_performance_pct,
        snap.sleep_duration_hrs,
        snap.strain,
    )
    return True


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else str(value)


def run_streaks() -> None:
    conn = open_db()
    rows = db.fetch_all(conn)
    if not rows:
        console.print("No history yet - run `today` a few times first.")
        return

    console.print(Panel.fit("Whoop – Streaks & Personal Bests", style="bold cyan"))

    all_streaks = _find_streaks(rows)
    all_streaks.sort(key=len, reverse=True)

    today_str = date.today().isoformat()
    yesterday_str = (date.today() - timedelta(days=1)).isoformat()

    active_streak: list[str] | None = None
    for streak in all_streaks:
        if streak[-1] in (today_str, yesterday_str):
            active_streak = streak
            break

    # Section 1: Current streak
    if active_streak:
        console.print(
            f"\n  [bold green]Current streak: {len(active_streak)} green day(s)[/]"
            f"  ({active_streak[0]} → {active_streak[-1]})"
        )
    else:
        by_date = {r["date"]: r["recovery_pct"] for r in rows}
        last_green = next(
            (d for d in sorted(by_date, reverse=True)
             if by_date[d] is not None and float(by_date[d]) >= 67.0),
            None,
        )
        if last_green:
            days_since = (date.today() - date.fromisoformat(last_green)).days
            console.print(
                f"\n  [dim]Current streak: 0 green days"
                f" — last green was {days_since} day(s) ago ({last_green})[/]"
            )
        else:
            console.print("\n  [dim]Current streak: 0 green days — no green days logged yet[/]")

    # Section 2: Top streaks table
    top = all_streaks[:3]
    if top:
        stbl = Table(title="Top Green Streaks", show_header=True, header_style="bold")
        stbl.add_column("Rank")
        stbl.add_column("Start")
        stbl.add_column("End")
        stbl.add_column("Days", justify="right")
        for i, streak in enumerate(top):
            is_active = active_streak is not None and streak == active_streak
            rank_label = f"#{i + 1}" + (" (active)" if is_active else "")
            row_style = "bold green" if is_active else ""
            stbl.add_row(rank_label, streak[0], streak[-1], str(len(streak)), style=row_style)
        console.print(stbl)

    # Section 3: Personal bests
    def _best(key: str, mode: str = "max") -> tuple[float, str] | None:
        pairs = [(float(r[key]), r["date"]) for r in rows if r[key] is not None]
        if not pairs:
            return None
        return max(pairs, key=lambda x: x[0]) if mode == "max" else min(pairs, key=lambda x: x[0])

    lines: list[str] = []
    if (v := _best("recovery_pct")):
        lines.append(f"Best recovery:    [green]{v[0]:.0f}%[/] on {v[1]}")
    if (v := _best("hrv_ms")):
        lines.append(f"Best HRV:         [cyan]{v[0]:.0f} ms[/] on {v[1]}")
    if (v := _best("sleep_duration_hrs")):
        lines.append(f"Best sleep:       [blue]{v[0]:.1f} hrs[/] on {v[1]}")
    if (v := _best("rhr_bpm", "min")):
        lines.append(f"Lowest RHR:       [magenta]{v[0]:.0f} bpm[/] on {v[1]}")
    if (v := _best("strain")):
        lines.append(f"Highest strain:   [yellow]{v[0]:.1f}[/] on {v[1]}")

    if lines:
        console.print(Panel("\n".join(lines), title="Personal Bests", border_style="cyan"))


def run_weekly(weeks: int) -> None:
    from collections import defaultdict

    conn = open_db()
    rows = db.fetch_all(conn)
    if not rows:
        console.print("No history yet - run `today` a few times first.")
        return

    console.print(Panel.fit("Whoop – Weekly Summary", style="bold cyan"))

    week_rows: dict = defaultdict(list)
    for r in rows:
        week_rows[_week_start(r["date"])].append(r)

    sorted_weeks = sorted(week_rows.keys(), reverse=True)[:weeks]

    tbl = Table(
        title=f"Weekly Summary (last {weeks} weeks)",
        show_header=True,
        header_style="bold",
    )
    tbl.add_column("Week")
    tbl.add_column("Avg Recovery", justify="right")
    tbl.add_column("Avg HRV", justify="right")
    tbl.add_column("Avg Sleep hrs", justify="right")
    tbl.add_column("Total Strain", justify="right")
    tbl.add_column("Days", justify="right")

    week_avgs: dict = {}

    for ws in sorted_weeks:
        week_data = week_rows[ws]
        days_n = len(week_data)

        rec_w = [float(r["recovery_pct"]) for r in week_data if r["recovery_pct"] is not None]
        hrv_w = [float(r["hrv_ms"]) for r in week_data if r["hrv_ms"] is not None]
        slp_w = [float(r["sleep_duration_hrs"]) for r in week_data if r["sleep_duration_hrs"] is not None]
        str_w = [float(r["strain"]) for r in week_data if r["strain"] is not None]

        avg_rec = sum(rec_w) / len(rec_w) if rec_w else None
        avg_hrv = sum(hrv_w) / len(hrv_w) if hrv_w else None
        avg_slp = sum(slp_w) / len(slp_w) if slp_w else None
        tot_str = sum(str_w) if str_w else None

        if avg_rec is not None and days_n >= 3:
            week_avgs[ws] = avg_rec

        style = recovery_style(avg_rec)
        label = f"{ws.strftime('%b')} {ws.day}"
        tbl.add_row(
            label,
            f"[{style}]{avg_rec:.0f}%[/]" if avg_rec is not None else "[dim]n/a[/]",
            f"{avg_hrv:.0f}" if avg_hrv is not None else "[dim]n/a[/]",
            f"{avg_slp:.1f}" if avg_slp is not None else "[dim]n/a[/]",
            f"{tot_str:.1f}" if tot_str is not None else "[dim]n/a[/]",
            f"{days_n}/7",
        )

    console.print(tbl)

    if week_avgs:
        best_ws = max(week_avgs, key=lambda w: week_avgs[w])
        worst_ws = min(week_avgs, key=lambda w: week_avgs[w])
        console.print(
            f"\n  [green]Best week:[/]  "
            f"{best_ws.strftime('%b')} {best_ws.day}"
            f" — avg {week_avgs[best_ws]:.0f}% recovery"
        )
        console.print(
            f"  [red]Worst week:[/] "
            f"{worst_ws.strftime('%b')} {worst_ws.day}"
            f" — avg {week_avgs[worst_ws]:.0f}% recovery"
        )


def run_trends(days: int | None) -> None:
    conn = open_db()
    rows = db.fetch_recent(conn, days) if days is not None else db.fetch_all(conn)
    if not rows:
        console.print("No history yet - run `today` a few times first.")
        return

    console.print(Panel.fit("Whoop – Trends", style="bold cyan"))

    hrv_vals = [r["hrv_ms"] for r in rows]
    rec_vals = [r["recovery_pct"] for r in rows]
    rhr_vals = [r["rhr_bpm"] for r in rows]

    # Section 1: Sparklines
    console.print("\n[bold]Sparklines[/]  (each character = one logged day)\n")
    console.print(f"  Recovery  {_sparkline(rec_vals)}")
    console.print(f"  HRV       {_sparkline(hrv_vals)}")
    console.print(f"  RHR       {_sparkline(rhr_vals)}")

    # Section 2: Rolling averages
    def _col(vals: list[float | None], window: int, is_recovery: bool = False) -> str:
        tail_non_none = [v for v in vals[-window:] if v is not None]
        avg = _rolling_avg(vals, window)
        if avg is None:
            return "[dim]n/a[/]"
        sparse = len(tail_non_none) < window
        suffix = f" [dim]({len(tail_non_none)}d)[/]" if sparse else ""
        if is_recovery:
            s = recovery_style(avg)
            return f"[{s}]{avg:.1f}[/]{suffix}"
        return f"{avg:.1f}{suffix}"

    tbl = Table(title="Rolling Averages", show_header=True, header_style="bold")
    tbl.add_column("Metric")
    tbl.add_column("7-day", justify="right")
    tbl.add_column("14-day", justify="right")
    tbl.add_column("28-day", justify="right")
    tbl.add_row("Recovery %",
                _col(rec_vals, 7, True), _col(rec_vals, 14, True), _col(rec_vals, 28, True))
    tbl.add_row("HRV ms",
                _col(hrv_vals, 7), _col(hrv_vals, 14), _col(hrv_vals, 28))
    tbl.add_row("RHR bpm",
                _col(rhr_vals, 7), _col(rhr_vals, 14), _col(rhr_vals, 28))
    console.print(tbl)

    # Section 3: Trend direction
    console.print("\n[bold]Trend  (last 7 days vs prior 7 days)[/]\n")
    console.print(f"  Recovery:  {_trend_arrow(rec_vals)}")
    console.print(f"  HRV:       {_trend_arrow(hrv_vals)}")


def run_history(n: int) -> None:
    conn = open_db()
    rows = db.fetch_recent(conn, n)
    if not rows:
        console.print("No history yet - run `today` a few times first.")
        return

    table = Table(title=f"Last {len(rows)} days")
    for col in ["Date", "Recovery", "HRV", "RHR", "Sleep %", "Sleep hrs", "Strain"]:
        table.add_column(col)

    for r in rows:
        style = recovery_style(r["recovery_pct"])
        table.add_row(
            r["date"],
            f"[{style}]{_fmt(r['recovery_pct'])}[/]",
            _fmt(r["hrv_ms"]),
            _fmt(r["rhr_bpm"]),
            _fmt(r["sleep_performance_pct"]),
            _fmt(r["sleep_duration_hrs"]),
            _fmt(r["strain"]),
        )
    console.print(table)


def heatmap_cell_style(pct: float | None) -> str:
    if pct is None:
        return "grey23"
    if pct >= 67:
        return "green"
    if pct >= 34:
        return "yellow"
    return "red"


def heatmap_text_style(pct: float | None) -> str:
    """Foreground color that reads clearly on top of the cell's background."""
    return "black" if pct is not None and pct >= 34 else "white"


def run_heatmap(max_weeks: int) -> None:
    conn = open_db()
    rows = db.fetch_all(conn)
    if not rows:
        console.print("No history yet - run `today` a few times first.")
        return

    by_date = {r["date"]: r["recovery_pct"] for r in rows}
    earliest = date.fromisoformat(min(by_date))
    today = date.today()

    # Anchor the grid on Mondays so weeks line up as Mon..Sun columns, but
    # don't pad out past as far as actual data goes - that just produces
    # rows of blank grey squares that make the real data harder to spot.
    end = today - timedelta(days=today.weekday())
    earliest_monday = earliest - timedelta(days=earliest.weekday())
    span_weeks = (end - earliest_monday).days // 7 + 1
    weeks = max(1, min(max_weeks, span_weeks))
    start = end - timedelta(weeks=weeks - 1)

    console.print(
        f"\n[bold]Recovery heatmap[/]  {start.isoformat()} -> {today.isoformat()}\n"
    )

    # Month labels above the week column where that month starts.
    month_row = Text("     ")
    last_month = None
    for w in range(weeks):
        col_date = start + timedelta(weeks=w)
        label = col_date.strftime("%b") if col_date.month != last_month else "   "
        last_month = col_date.month
        month_row.append(f"{label:<4}")
    console.print(month_row)

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for dow in range(7):
        line = Text(f"{day_names[dow]} ")
        for w in range(weeks):
            cell_date = start + timedelta(weeks=w, days=dow)
            if cell_date > today:
                line.append("    ")  # future - leave blank, nothing to show
                continue
            pct = by_date.get(cell_date.isoformat())
            bg = heatmap_cell_style(pct)
            label = f"{pct:>2.0f}" if pct is not None else " ."
            fg = heatmap_text_style(pct)
            line.append(f" {label} ", style=f"{fg} on {bg}")
        console.print(line)

    recent = [r["recovery_pct"] for r in rows if r["recovery_pct"] is not None]
    if recent:
        avg = sum(recent) / len(recent)
        green_days = sum(1 for r in rows if (r["recovery_pct"] or 0) >= 67)
        console.print(
            f"\n[dim]{len(rows)} day(s) logged - avg recovery {avg:.0f}% - "
            f"{green_days} green day(s)[/]"
        )
    console.print(
        "\nEach square is that day's recovery %.  "
        "[black on green] 80 [/] 67%+   "
        "[black on yellow] 50 [/] 34-66%   "
        "[white on red] 20 [/] <34%   "
        "[white on grey23]  . [/] no data logged"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

USAGE = "Usage: whoop_daily.py (setup | today | history [--days N] | heatmap [--weeks N] | correlate [--days N] | trends [--days N] | weekly [--weeks N] | streaks)"


def main() -> None:
    setup_logging()
    if len(sys.argv) < 2:
        console.print(USAGE, markup=False)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "setup":
        run_setup()
    elif cmd == "today":
        if not run_today():
            sys.exit(1)
    elif cmd == "history":
        days = 14
        if "--days" in sys.argv:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        run_history(days)
    elif cmd == "heatmap":
        weeks = 12
        if "--weeks" in sys.argv:
            weeks = int(sys.argv[sys.argv.index("--weeks") + 1])
        run_heatmap(weeks)
    elif cmd == "correlate":
        days = None
        if "--days" in sys.argv:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        run_correlate(days)
    elif cmd == "trends":
        days = None
        if "--days" in sys.argv:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        run_trends(days)
    elif cmd == "weekly":
        weeks = 8
        if "--weeks" in sys.argv:
            weeks = int(sys.argv[sys.argv.index("--weeks") + 1])
        run_weekly(weeks)
    elif cmd == "streaks":
        run_streaks()
    else:
        console.print(f"Unknown command: {cmd}", markup=False)
        console.print(USAGE, markup=False)
        sys.exit(1)


if __name__ == "__main__":
    main()
