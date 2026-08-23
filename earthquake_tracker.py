import logging
import sys
from datetime import datetime, timezone
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
    """Downloads and parses the USGS real-time earthquake GeoJSON feed."""
    response = requests.get(USGS_FEED_URL, timeout=30)
    response.raise_for_status()
    return response.json()


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


def insert_events(conn, events):
    """Inserts new earthquake events. Keyed on USGS's own event id, so
    re-processing the overlapping 24h window every 15 minutes never creates
    duplicates. Note: USGS revises magnitude/location for a period after an
    event; ON CONFLICT DO NOTHING means a row keeps the values from when it
    was first seen, not later revisions - same insert-only behavior as
    conflict_map_tracker.py."""
    ingested_at = datetime.now(timezone.utc)

    inserted = 0
    with conn.cursor() as cur:
        for event in events:
            cur.execute(
                """
                INSERT INTO earthquake_events
                    (event_id, event_time, ingested_at, place, mag, mag_type, depth_km,
                     lat, long, significance, alert, tsunami, event_type, status, source_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    event["event_id"], event["event_time"], ingested_at, event["place"],
                    event["mag"], event["mag_type"], event["depth_km"], event["lat"], event["long"],
                    event["significance"], event["alert"], event["tsunami"], event["event_type"],
                    event["status"], event["source_url"],
                ),
            )
            inserted += cur.rowcount

    conn.commit()
    return inserted


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
            new_count = insert_events(conn, matched)
    except Exception as e:
        log.error(f"Error writing to PostgreSQL: {e}")
        sys.exit(1)

    log.info(f"Done. {new_count} new earthquake events inserted (of {len(matched)} matched this run).")
