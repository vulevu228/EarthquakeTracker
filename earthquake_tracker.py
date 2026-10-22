import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import requests

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "earthquake_tracker.log"

# Connects as the local 'postgres' user with no password argument - libpq
# picks the password up from %APPDATA%\postgresql\pgpass.conf, so it never
# has to live in source code or in the Scheduled Task definition.
PG_CONN_PARAMS = {
    "host": "localhost",
    "port": 5432,
    "dbname": "earthquake_tracker",
    "user": "postgres",
}

handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=handlers,
)
log = logging.getLogger(__name__)

# USGS's real-time GeoJSON summary feed - free, no API key, updated roughly
# every minute. "all_day" (trailing 24h) rather than "all_hour" (trailing
# 60min) so a 15-minute cron has a wide overlap margin - same reasoning as
# conflict_map_tracker.py's overlapping GDELT windows, just simpler since
# USGS does the windowing server-side instead of 15-minute chunk files.
USGS_FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_month.geojson"

# USGS's feed also includes non-tectonic seismic detections (mining/quarry
# blasts, explosions) under the same feed - filtered out here since they
# aren't earthquakes, same spirit as GDELT's QuadClass==4 filter.
EVENT_TYPE_FILTER = "earthquake"


def fetch_feed():
    """Downloads and parses the USGS real-time earthquake GeoJSON feed. The
    month feed is ~10 MB, so a slow read or dropped connection gets two more
    tries before the run counts as failed."""
    for attempt in range(3):
        try:
            response = requests.get(USGS_FEED_URL, timeout=60)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == 2:
                raise
            log.info(f"USGS feed attempt {attempt + 1}/3 failed ({type(e).__name__}), retrying")
            time.sleep(10 * (attempt + 1))


def extract_earthquake_events(geojson):
    """Filters GeoJSON features down to real earthquakes and flattens the
    fields we care about. Coordinates arrive pre-geocoded from USGS as
    [longitude, latitude, depth_km] - no geocoding step needed."""
    events = []
    for feature in geojson.get("features", []):
        props = feature.get("properties") or {}
        if props.get("type") != EVENT_TYPE_FILTER:
            continue

        coords = (feature.get("geometry") or {}).get("coordinates")
        if not coords or len(coords) < 3:
            continue
        lon, lat, depth_km = coords[0], coords[1], coords[2]

        event_id = feature.get("id")
        mag = props.get("mag")
        event_time_ms = props.get("time")
        if not event_id or mag is None or event_time_ms is None:
            continue

        events.append(
            {
                "event_id": event_id,
                "event_time": datetime.fromtimestamp(event_time_ms / 1000, tz=timezone.utc),
                "place": props.get("place"),
                "mag": float(mag),
                "mag_type": props.get("magType"),
                "depth_km": float(depth_km),
                "lat": float(lat),
                "long": float(lon),
                "significance": props.get("sig"),
                "alert": props.get("alert"),
                "tsunami": props.get("tsunami"),
                "event_type": props.get("type"),
                "status": props.get("status"),
                "source_url": props.get("url"),
            }
        )

    return events


def init_db(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS earthquake_events (
                event_id         TEXT PRIMARY KEY,
                event_time       TIMESTAMPTZ,
                ingested_at      TIMESTAMPTZ,
                place            TEXT,
                mag              DOUBLE PRECISION,
                mag_type         TEXT,
                depth_km         DOUBLE PRECISION,
                lat              DOUBLE PRECISION,
                long             DOUBLE PRECISION,
                significance     INTEGER,
                alert            TEXT,
                tsunami           INTEGER,
                event_type       TEXT,
                status           TEXT,
                source_url       TEXT
            )
            """
        )
    conn.commit()


REVISABLE = ("event_time", "place", "mag", "mag_type", "depth_km", "lat", "long",
             "significance", "alert", "tsunami", "event_type", "status", "source_url")


def upsert_events(conn, events):
    """Inserts new earthquake events and applies USGS's revisions to known
    ones. Keyed on USGS's own event id, so re-processing the month feed every
    15 minutes never creates duplicates. USGS revises magnitude, location and
    status (automatic -> reviewed) for days after an event, so those fields
    follow the feed; ingested_at stays the time the event was first seen.
    Returns (inserted, revised)."""
    ingested_at = datetime.now(timezone.utc)
    cols = ", ".join(REVISABLE)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in REVISABLE)

    inserted = revised = 0
    with conn.cursor() as cur:
        for event in events:
            cur.execute(
                f"""
                INSERT INTO earthquake_events (event_id, ingested_at, {cols})
                VALUES (%s, %s, {", ".join(["%s"] * len(REVISABLE))})
                ON CONFLICT (event_id) DO UPDATE SET {updates}
                WHERE ({", ".join(f"earthquake_events.{c}" for c in REVISABLE)})
                      IS DISTINCT FROM ({", ".join(f"EXCLUDED.{c}" for c in REVISABLE)})
                RETURNING (xmax = 0)
                """,
                (event["event_id"], ingested_at, *(event[c] for c in REVISABLE)),
            )
            row = cur.fetchone()  # None: known and unchanged
            if row:
                inserted += row[0]
                revised += not row[0]

    conn.commit()
    return inserted, revised


def remove_withdrawn(conn, events):
    """Rows inside the feed's window that the feed no longer lists as an
    earthquake were deleted by USGS, merged into another network's id (the
    same quake would otherwise count twice) or reclassified (quarry blast).
    They are moved to earthquake_events_removed rather than dropped."""
    if len(events) < 1000:  # a truncated or empty feed must not empty the table
        log.info(f"Only {len(events)} events in the feed - skipping the withdrawn-event check")
        return 0
    window_start = min(e["event_time"] for e in events) + timedelta(hours=1)
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS earthquake_events_removed
                (LIKE earthquake_events, removed_at TIMESTAMPTZ)
            """
        )
        cur.execute(
            """
            WITH gone AS (
                DELETE FROM earthquake_events
                WHERE event_time >= %s AND NOT (event_id = ANY(%s))
                RETURNING *
            )
            INSERT INTO earthquake_events_removed SELECT *, now() FROM gone
            """,
            (window_start, [e["event_id"] for e in events]),
        )
        removed = cur.rowcount
    conn.commit()
    return removed


if __name__ == "__main__":
    log.info("Fetching USGS real-time earthquake feed...")

    try:
        feed = fetch_feed()
        matched = extract_earthquake_events(feed)
        log.info(f"{len(feed.get('features', []))} features fetched, {len(matched)} matched as earthquakes")
    except Exception as e:
        # Exit non-zero rather than silently continuing with an empty batch -
        # otherwise a real outage (network/USGS down) logs identically to a
        # genuinely quiet run ("0 new events"), and Task Scheduler would show
        # a false "success" for a run that fetched nothing.
        log.error(f"Error fetching USGS feed: {e}")
        sys.exit(1)

    try:
        with psycopg2.connect(**PG_CONN_PARAMS) as conn:
            init_db(conn)
            new_count, revised = upsert_events(conn, matched)
            removed = remove_withdrawn(conn, matched)
    except Exception as e:
        log.error(f"Error writing to PostgreSQL: {e}")
        sys.exit(1)

    log.info(f"Done. {new_count} new earthquake events inserted, {revised} revised by USGS, "
             f"{removed} withdrawn (of {len(matched)} matched this run).")
