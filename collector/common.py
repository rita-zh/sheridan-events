"""Helpers that turn source data into the event shape the API serves.

Every event looks like this (same fields the old scrapers + server produced):

  id, source, source_name, source_url, start_date, end_date, time_24h,
  title, url, image_url, list_date_box, excerpt, last_occurrence_ends_at,
  occurrences, address {venue, lines, map_url}, categories, tags,
  google_calendar_url, ics_url, description, month, day, time, venue,
  ...plus a few source-specific extras (organizations, price, is_online...)
"""

import html
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

LOCAL_TZ = ZoneInfo("America/Toronto")


class CollectorError(Exception):
    """A source could not be collected; the runner keeps its previous data."""


# ---------- text ----------

def clean(text):
    if not isinstance(text, str):
        return text
    # Narrow / regular no-break spaces show up in times like "4:00 PM"
    text = html.unescape(text).replace(" ", " ").replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def html_to_text(markup):
    """One line per paragraph, so bold/italic words stay inside their sentence."""
    if not markup:
        return ""
    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    blocks = soup.find_all(["p", "li", "h1", "h2", "h3", "h4"])
    if not blocks:
        return clean(soup.get_text(" ", strip=True))
    lines = (clean(" ".join(block.get_text(" ").split())) for block in blocks)
    return "\n".join(dict.fromkeys(line for line in lines if line))


def shorten(text, limit=200):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


# ---------- dates ----------

def now_utc():
    return datetime.now(timezone.utc)


def today_local():
    return datetime.now(LOCAL_TZ).date()


def to_local(value):
    """Aware datetime, ISO string, or epoch milliseconds -> Toronto datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Squarespace adds stray milliseconds (…T00:00:00.920Z); drop them
        return datetime.fromtimestamp(int(value // 1000), tz=timezone.utc).astimezone(LOCAL_TZ)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(LOCAL_TZ)


def to_12h(dt):
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def occurrence(day, start=None):
    """One entry of event.occurrences. start=None means the time isn't known."""
    return {
        "date": day.isoformat(),
        "date_text": f"{day:%A, %B} {day.day}, {day.year}",
        "time_12h": to_12h(start) if start else None,
        "time_24h": f"{start:%H:%M}" if start else None,
    }


def google_calendar_url(title, start, end, location):
    start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    dates = f"{start_utc:%Y%m%dT%H%M%SZ}/{end_utc:%Y%m%dT%H%M%SZ}"
    return (
        "http://www.google.com/calendar/event?action=TEMPLATE"
        f"&text={quote(title)}&dates={dates}&location={quote(location or '')}"
    )


def map_url(query=None, lat=None, lng=None):
    try:
        if lat is not None and lng is not None and (float(lat), float(lng)) != (0.0, 0.0):
            return f"http://maps.google.com?q={float(lat)},{float(lng)}"
    except (TypeError, ValueError):
        pass
    return f"http://maps.google.com?q={quote(query)}" if query else None


# ---------- the event record ----------

def slug_from_url(url):
    path = urlparse(url).path
    return [part for part in path.split("/") if part][-1] if path.strip("/") else "event"


def build_event(
    *,
    title,
    url,
    start,
    end=None,
    time_known=True,
    extra_days=(),
    image_url=None,
    description="",
    excerpt=None,
    address=None,
    categories=(),
    tags=(),
    ics_url=None,
    **extras,
):
    """Build the per-source record (the shape the old scrapers wrote).

    start / end are aware datetimes. extra_days are more dates listed for the
    event (e.g. Sheridan's "Nov 17 – Jan 19" shows both ends as occurrences).
    """
    title = clean(title)
    start = to_local(start)
    end = to_local(end) or start
    if end < start:
        end = start
    address = address or {"venue": "Location not listed", "lines": [], "map_url": None}
    description = (description or "").strip()
    excerpt = clean(excerpt) if excerpt else shorten(description.replace("\n", " "))

    days = list(dict.fromkeys([start.date(), *extra_days]))
    occurrences = [occurrence(days[0], start if time_known else None)]
    occurrences += [occurrence(day, start if time_known else None) for day in days[1:]]

    return {
        "title": title,
        "url": url,
        "image_url": image_url or None,
        "list_date_box": {"month": start.strftime("%b"), "day": str(start.day)},
        "excerpt": excerpt,
        "last_occurrence_ends_at": end.astimezone(timezone.utc).isoformat(),
        "occurrences": occurrences,
        "address": address,
        "categories": list(dict.fromkeys(c for c in categories if c)),
        "tags": list(dict.fromkeys(t for t in tags if t)),
        "google_calendar_url": google_calendar_url(title, start, end, ", ".join(address.get("lines") or [])),
        "ics_url": ics_url,
        "description": description,
        **extras,
    }


def end_date_of(record):
    """Local date the event ends. An end at exactly midnight belongs to the day before."""
    end = to_local(record["last_occurrence_ends_at"])
    start_date = record["occurrences"][0]["date"]
    if end.hour == 0 and end.minute == 0 and end.date().isoformat() > start_date:
        end -= timedelta(minutes=1)
    return max(end.date().isoformat(), record["occurrences"][-1]["date"])


def finalize(record, source):
    """Add the fields the API serves on top of the per-source record."""
    first = record["occurrences"][0]
    return {
        "id": f"{source.key}-{slug_from_url(record['url'])}",
        "source": source.key,
        "source_name": source.name,
        "source_url": source.url,
        "start_date": first["date"],
        "end_date": end_date_of(record),
        **record,
        # Flat fields the front-end reads directly (were added by normalize_events.py)
        "month": record["list_date_box"]["month"],
        "day": record["list_date_box"]["day"],
        "time": first["time_12h"],
        "venue": record["address"].get("venue"),
        "time_24h": first["time_24h"],
    }


def is_upcoming(event, today=None):
    today = (today or today_local()).isoformat()
    return (event.get("end_date") or event.get("start_date") or "") >= today


def parse_day(text):
    """'Jun. 16, 2026' / 'Sept. 3, 2026' / 'November 7, 2026' -> date"""
    month, rest = clean(text).replace(".", "").split(" ", 1)
    return datetime.strptime(f"{month[:3]} {rest.replace(',', ', ').replace('  ', ' ')}", "%b %d, %Y").date()


def add_months(day, months):
    month_index = day.month - 1 + months
    return date(day.year + month_index // 12, month_index % 12 + 1, 1)
