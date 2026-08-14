import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# Resolve paths relative to this file so behavior doesn't depend on CWD.
BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "earthquake_events.db"
LOG_FILE = BASE_DIR / "earthquake_tracker.log"

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
# 60min) so an hourly cron has a wide overlap margin - same reasoning as
# conflict_map_tracker.py's overlapping GDELT windows, just simpler since
# USGS does the windowing server-side instead of 15-minute chunk files.
USGS_FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"

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
                "event_time": datetime.fromtimestamp(event_time_ms / 1000, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS earthquake_events (
            event_id         TEXT PRIMARY KEY,
            event_time       TEXT,
            ingested_at      TEXT,
            place            TEXT,
            mag              REAL,
            mag_type         TEXT,
            depth_km         REAL,
            lat              REAL,
            long             REAL,
            significance     INTEGER,
            alert            TEXT,
            tsunami          INTEGER,
            event_type       TEXT,
            status           TEXT,
            source_url       TEXT
        )
        """
    )
    conn.commit()


def insert_events(conn, events):
    """Inserts new earthquake events. Keyed on USGS's own event id, so
    re-processing the overlapping 24h window every hour never creates
    duplicates. Note: USGS revises magnitude/location for a period after an
    event; INSERT OR IGNORE means a row keeps the values from when it was
    first seen, not later revisions - same insert-only behavior as
    conflict_map_tracker.py."""
    ingested_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    inserted = 0
    for event in events:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO earthquake_events
                (event_id, event_time, ingested_at, place, mag, mag_type, depth_km,
                 lat, long, significance, alert, tsunami, event_type, status, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"], event["event_time"], ingested_at, event["place"],
                event["mag"], event["mag_type"], event["depth_km"], event["lat"], event["long"],
                event["significance"], event["alert"], event["tsunami"], event["event_type"],
                event["status"], event["source_url"],
            ),
        )
        inserted += cursor.rowcount

    conn.commit()
    return inserted


if __name__ == "__main__":
    log.info("Fetching USGS real-time earthquake feed...")

    try:
        feed = fetch_feed()
        matched = extract_earthquake_events(feed)
        log.info(f"{len(feed.get('features', []))} features fetched, {len(matched)} matched as earthquakes")
    except Exception as e:
        log.error(f"Error fetching USGS feed: {e}")
        matched = []

    with sqlite3.connect(DB_FILE) as conn:
        init_db(conn)
        new_count = insert_events(conn, matched)

    log.info(f"Done. {new_count} new earthquake events inserted (of {len(matched)} matched this run).")
