# whoop-daily

A tiny CLI for pulling your Whoop recovery/sleep/strain and keeping a local
trend log. No cloud — just a JSON config and a SQLite database in
`~/.whoop-daily/`.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Register a developer app at https://developer.whoop.com to get a
   **Client ID** and **Client Secret**. Set the app's redirect URI to:
   ```
   http://localhost:8765/callback
   ```
3. Run the one-time login:
   ```
   python whoop_daily.py setup
   ```
   This opens your browser, you log into Whoop, and the tool saves your
   access/refresh tokens to `~/.whoop-daily/config.json`.

## Usage

```
python whoop_daily.py today      # pull today's recovery/sleep/strain, log it
python whoop_daily.py history    # show the last 14 days
python whoop_daily.py history --days 30
python whoop_daily.py heatmap    # GitHub-style contribution grid, colored by recovery
python whoop_daily.py heatmap --weeks 26
python whoop_daily.py correlate           # correlation analysis across all logged data
python whoop_daily.py correlate --days 30 # limit to last N days
python whoop_daily.py trends             # sparklines + rolling averages + trend direction for recovery, HRV, RHR
python whoop_daily.py trends --days 60
python whoop_daily.py weekly             # week-by-week summary table (avg recovery, HRV, sleep, strain)
python whoop_daily.py weekly --weeks 12
python whoop_daily.py streaks            # current green streak, top streaks, personal bests
```

`heatmap` renders a Mon-Sun grid (like a GitHub contributions graph) where each
day is colored by recovery score: green (67%+), yellow (34-66%), red (<34%),
grey (no data / future). Good for a 2-second glance at recovery trend instead
of reading numbers.

`correlate` runs a statistical analysis on your logged data and prints a terminal
report with three sections:

1. **Strain → Next-Day Recovery** — buckets your strain scores (Low / Moderate /
   High / All-out) and shows the average next-day recovery for each, plus a
   Pearson correlation with a two-tailed t-test.
2. **Sleep Duration → Recovery** — same idea bucketed by sleep hours (<6 h, 6–7 h,
   7–8 h, 8–9 h, 9 h+), with its own correlation test.
3. **Multiple Regression** — OLS model predicting next-day recovery from
   yesterday's strain and last night's sleep duration together, with coefficient
   t-tests, an overall F-test, R², and a VIF multicollinearity check.

Requires at least 10 complete data points per section; shows a friendly warning
and skips that section if you don't have enough data yet. `scipy` is the only
non-stdlib dependency added for this command (distribution CDFs only — all matrix
ops are pure Python).

`trends` shows three sections: a text sparkline for Recovery, HRV, and RHR (one character per
logged day using `▁▂▃▄▅▆▇█`); a rolling-averages table with 7/14/28-day windows; and a trend
direction line (↑ improving / ↓ declining / → flat) comparing your last 7 days to the prior 7.

`weekly` shows a week-by-week summary table (Mon–Sun rows, newest first). Columns: week start,
avg recovery (color-coded), avg HRV, avg sleep hours, total strain, and days logged out of 7.
A best/worst week callout appears below the table (only weeks with 3+ logged days qualify).

`streaks` shows your current consecutive green-day streak (recovery ≥ 67%), your top 3 longest
green streaks ever, and a personal bests panel (best recovery, best HRV, best sleep, lowest RHR,
highest strain — each with the date it occurred).

## Scheduled run (Windows Task Scheduler)

`today` can be run automatically every night via a Task Scheduler task.

- **Task name:** `WhoopDaily` (top-level, no folder)
- **Trigger:** daily at 11:30 PM
- **Action:** `C:\path\to\python.exe "C:\path\to\whoop-daily\whoop_daily.py" today`
- **Working directory:** `C:\path\to\whoop-daily`

### Checking whether last night's run worked

1. **Task Scheduler's own status** — open Task Scheduler, find `WhoopDaily`,
   check the **Last Run Result** column. `0x0` (or "The operation completed
   successfully") means the script exited cleanly. Anything else means it
   exited non-zero. Same thing from PowerShell:
   ```powershell
   Get-ScheduledTaskInfo -TaskName "WhoopDaily" | Select-Object LastRunTime, LastTaskResult
   ```
   `today` exits with status code `1` if it isn't set up (no token) or the
   Whoop API call fails/raises, and `0` on success — so a non-zero
   `LastTaskResult` reliably flags a failed run.

2. **The log file** — `~/.whoop-daily/run.log` has a timestamped line from every
   run:
   ```
   type %USERPROFILE%\.whoop-daily\run.log
   ```
   or in PowerShell: `Get-Content $env:USERPROFILE\.whoop-daily\run.log -Tail 20`
   - `INFO ... today: ok date=...` → success, with the metrics that were logged
   - `WARNING ... today: not set up yet` → no token (run `setup` again)
   - `ERROR ... today: Whoop API error: ...` → API call failed (4xx/5xx)
   - `ERROR ... today: unexpected error` (with a traceback) → something else broke

3. **The database** — confirm a row landed for today's date:
   ```
   sqlite3 %USERPROFILE%\.whoop-daily\whoop.db "select * from daily_metrics order by date desc limit 1;"
   ```

If Task Scheduler shows a non-zero result but `run.log` has no corresponding
entry, the failure happened before logging was set up (e.g. Python/path
issue) — check Task Scheduler's history tab for that run instead.

## Notes

- Tokens auto-refresh; you should only need to run `setup` once (unless you
  revoke access on Whoop's end).
- `~/.whoop-daily/config.json` holds your tokens — don't commit or share it.
- Daily metrics are stored in `~/.whoop-daily/whoop.db` (SQLite), one row per
  day, keyed by date. Each row also stores the raw JSON returned by the
  recovery/sleep/cycle API calls (`recovery_raw`, `sleep_raw`, `cycle_raw`
  columns) so future analysis can pull fields beyond the flattened summary
  without a schema change. Query it directly with any SQLite client, e.g.
  `sqlite3 ~/.whoop-daily/whoop.db "select * from daily_metrics"`.
- If you have an old `~/.whoop-daily/log.csv` from a previous version, it's
  imported into the database automatically the first time you run `today` or
  `history`, then renamed to `log.csv.bak`.
- The Whoop API has changed shape over the years; if `today` errors out with
  a 4xx, the API version/endpoint paths in `whoop_daily.py` (`API_BASE`,
  `/recovery`, `/activity/sleep`, `/cycle`) may need a small update to match
  whatever the developer portal documents at signup time.
