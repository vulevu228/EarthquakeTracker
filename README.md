# 🌍 Earthquake Seismic Tracker

Passive pipeline logging real-time global earthquake events for a Power BI bubble map.

## 🗃️ Data Feed

| | `earthquake_events.db` |
|---|---|
| Script | `earthquake_tracker.py` |
| Cadence | Every hour |
| Covers | Real, tectonic earthquakes worldwide (magnitude, depth, location, significance, tsunami/alert flags) |
| Feeds | A Power BI bubble map (see below) |

Sourced from the [USGS real-time earthquake feed](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php) — free, already-geocoded, updated roughly every minute, no API key required. The tracker pulls the `all_day` feed (trailing 24 hours) on every hourly run rather than `all_hour` (trailing 60 minutes): USGS does the windowing server-side, and the wider window gives a large overlap margin so a missed or late cron run never loses events — the same reasoning as the [tracking_metals](../tracking_metals) repo's `conflict_map_tracker.py`, which pulls overlapping 15-minute GDELT windows for the same reason.

Each row is keyed on USGS's own event `id`, so re-running the tracker (or its 24h windows overlapping across hourly runs) never creates duplicates.

The feed also includes non-tectonic seismic detections (quarry blasts, explosions) under the same endpoint — filtered out by `properties.type == "earthquake"` before anything is stored.

**Known limitation:** USGS revises magnitude and location for a period after an event as more seismic stations report in. Inserts are `INSERT OR IGNORE`, so a row keeps the values from when it was first seen, not later revisions — same insert-only behavior as `conflict_map_tracker.py` in the sibling repo. A future pass could switch to `INSERT OR REPLACE` keyed on `event_id` if tracking revisions turns out to matter.

### Schema (`earthquake_events`)

| Column | Source | Notes |
|---|---|---|
| `event_id` (PK) | `id` | USGS event id, e.g. `us6000tkig` |
| `event_time` | `properties.time` | ISO 8601 UTC, converted from epoch ms |
| `ingested_at` | — | When this tracker's run inserted the row |
| `place` | `properties.place` | Human-readable location |
| `mag` | `properties.mag` | **Magnitude — use as severity/bubble size** |
| `mag_type` | `properties.magType` | e.g. `mb`, `ml`, `mww` |
| `depth_km` | `geometry.coordinates[2]` | |
| `lat` / `long` | `geometry.coordinates[1]` / `[0]` | Pre-geocoded by USGS |
| `significance` | `properties.sig` | USGS's own 0–1000+ composite severity score |
| `alert` | `properties.alert` | PAGER alert level: green/yellow/orange/red, or NULL |
| `tsunami` | `properties.tsunami` | 1 if a tsunami warning was associated |
| `event_type` | `properties.type` | Always `earthquake` post-filter |
| `status` | `properties.status` | `reviewed` or `automatic` |
| `source_url` | `properties.url` | Link to the USGS event page |

## 📈 Power BI

Not yet wired up — planned as a **bubble map** visual:
* **Location:** `lat` / `long`
* **Bubble size:** `mag`
* **Color:** magnitude bucket (e.g. <4 minor, 4–6 moderate, 6+ major) or `alert`/`significance` for a PAGER-style severity view
* **Tooltip candidates:** `place`, `event_time`, `depth_km`, `mag_type`, `tsunami`

Same connection pattern as `tracking_metals`' `.pbix`: point Power BI at `earthquake_events.db` via the same ODBC SQLite driver, refresh with `git pull` then Refresh after a tracker update.

## ⏰ Scheduling

* **Cloud (primary):** [`.github/workflows/earthquake_tracker.yml`](.github/workflows/earthquake_tracker.yml) — hourly cron (`0 * * * *`), commits `earthquake_events.db` back to the repo.
* **Local fallback:** `register-earthquake-task.ps1` (gitignored, machine-specific) — registers an hourly Windows Scheduled Task running `earthquake_tracker.py` via `pythonw.exe`, mirroring `tracking_metals/register-live-prices-task.ps1`.
