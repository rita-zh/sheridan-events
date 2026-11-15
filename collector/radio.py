"""SheridanLife Radio events from sheridanliferadio.ca/events.

The events page embeds a public Google Calendar. Google publishes every public
calendar as an iCalendar feed, so no scraping is needed:

  https://calendar.google.com/calendar/ical/<calendar id>/public/basic.ics

The calendar id is read from the page's embed (so it keeps working if they
switch calendars), with the one seen in Nov 2026 as a fallback.

Repeating events (e.g. the weekly volunteer meeting) are expanded into their
real dates, including skipped or moved weeks. Only the next few of each series
are kept so a weekly meeting doesn't fill the whole feed.
"""

import base64
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote, urlparse

import icalendar
import recurring_ical_events
import requests
from bs4 import BeautifulSoup

from .common import LOCAL_TZ, CollectorError, build_event, clean, html_to_text, map_url, today_local

log = logging.getLogger(__name__)

EVENTS_URL = "https://www.sheridanliferadio.ca/events"
DEFAULT_CALENDAR_ID = "032e2f554e4e3c99b91c6313aa763070ca45ad8f6af9cbcc1178962fa428103e@group.calendar.google.com"
FEED_URL = "https://calendar.google.com/calendar/ical/{}/public/basic.ics"
DAYS_AHEAD = 120
REPEATS_KEPT = 4  # per repeating series

# Street addresses Google Calendar shows -> campus names the site filters on
CAMPUSES = {
    "trafalgar": "Trafalgar Campus",
    "duke of york": "Hazel McCallion Campus",
    "mclaughlin": "Davis Campus",
}


def calendar_ids(http):
    """Calendar ids from the embed's src= params (plain or base64)."""
    try:
        soup = BeautifulSoup(http.text(EVENTS_URL), "html.parser")
    except requests.RequestException as exc:
        log.warning("radio: couldn't read the events page (%s); using the saved calendar id", exc)
        return [DEFAULT_CALENDAR_ID]
    ids = []
    for frame in soup.find_all("iframe", src=re.compile(r"calendar\.google\.com/calendar/embed")):
        for src in parse_qs(urlparse(frame["src"]).query).get("src", []):
            if "@" not in src:
                try:
                    src = base64.b64decode(src + "=" * (-len(src) % 4)).decode()
                except (ValueError, UnicodeDecodeError):
                    continue
            if "@" in src and src not in ids:
                ids.append(src)
    if not ids:
        log.warning("radio: no Google Calendar embed on the page; using the saved calendar id")
    return ids or [DEFAULT_CALENDAR_ID]


def local_moment(value):
    """(aware Toronto datetime, time known?) from an iCal date or datetime."""
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=LOCAL_TZ)
        return value.astimezone(LOCAL_TZ), True
    return datetime(value.year, value.month, value.day, tzinfo=LOCAL_TZ), False


def address_for(location):
    location = clean(location or "")
    if not location:
        return {"venue": "SheridanLife Radio", "lines": [], "map_url": None}, []
    campus = next((name for key, name in CAMPUSES.items() if key in location.lower()), None)
    lines = [campus, location] if campus else [part.strip() for part in location.split(",")]
    return {"venue": campus or lines[0], "lines": lines, "map_url": map_url(location)}, [campus] if campus else []


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "event"


def build(component, calendar_id):
    title = clean(str(component.get("SUMMARY") or "")) or "SheridanLife Radio event"
    start, time_known = local_moment(component.decoded("DTSTART"))
    if "DTEND" in component:
        end, _ = local_moment(component.decoded("DTEND"))
        if not time_known:  # all-day: DTEND is the day after
            end = end - timedelta(minutes=1)
    else:
        end = start + timedelta(hours=1) if time_known else start.replace(hour=23, minute=59)
    address, campus_tags = address_for(str(component.get("LOCATION") or ""))
    description = html_to_text(str(component.get("DESCRIPTION") or ""))
    return build_event(
        title=title,
        url=EVENTS_URL,
        start=start,
        end=end,
        time_known=time_known,
        extra_days=[end.date()] if end.date() != start.date() else [],
        description=description,
        address=address,
        tags=campus_tags,
        is_online=False,
        # One page holds every event, so the id comes from the title and date
        slug=f"{slug(title)}-{start:%Y-%m-%d}",
        ics_url=FEED_URL.format(quote(calendar_id)),
    )


def collect(http):
    today = today_local()
    window = (today, today + timedelta(days=DAYS_AHEAD))
    events, failures = [], []
    for calendar_id in calendar_ids(http):
        try:
            feed = icalendar.Calendar.from_ical(http.text(FEED_URL.format(quote(calendar_id))))
        except (requests.RequestException, ValueError) as exc:
            failures.append(f"{calendar_id}: {exc}")
            continue
        seen_per_series = {}
        occurrences = sorted(
            recurring_ical_events.of(feed).between(*window),
            key=lambda c: local_moment(c.decoded("DTSTART"))[0],
        )
        for component in occurrences:
            if str(component.get("STATUS") or "").upper() == "CANCELLED":
                continue
            uid = str(component.get("UID") or component.get("SUMMARY"))
            seen_per_series[uid] = seen_per_series.get(uid, 0) + 1
            if seen_per_series[uid] > REPEATS_KEPT:
                continue
            try:
                events.append(build(component, calendar_id))
            except (KeyError, ValueError) as exc:
                log.warning("radio: skipped %s: %s", component.get("SUMMARY"), exc)
    if failures and not events:
        raise CollectorError("couldn't read the Google Calendar feed: " + "; ".join(failures))
    log.info("radio: %d upcoming events in the next %d days", len(events), DAYS_AHEAD)
    return events
