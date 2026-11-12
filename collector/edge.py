"""EDGE events: the Eventbrite API, with a scraper fallback.

1. API (preferred):
     GET https://www.eventbriteapi.com/v3/organizers/13162231152/events/
         ?status=live&order_by=start_asc&expand=venue,ticket_availability,category,format
   (ticket_availability/category/format are extra expansions that keep the price,
   sold-out status and tags the old scraper produced.)
   The token comes from the EVENTBRITE_TOKEN environment variable. It is only
   sent in the Authorization header: never logged, never written to output.

2. Fallback (no token, or the API fails): the public organizer page embeds its
   upcoming events as JSON in <script id="__NEXT_DATA__"> (props.pageProps.
   upcomingEvents), and each event page has schema.org ld+json with the exact
   start/end times and venue. Plain HTTP, no browser needed.
"""

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from .common import CollectorError, build_event, html_to_text, map_url, to_local

log = logging.getLogger(__name__)

API = "https://www.eventbriteapi.com/v3"
ORGANIZER_ID = "13162231152"
ORGANIZER_URL = f"https://www.eventbrite.ca/o/edge-{ORGANIZER_ID}"
# "live" = upcoming and on sale; "started" = already running (multi-day events)
STATUSES = ("live", "started")
MAX_PAGES = 20


def local_time(moment):
    """Eventbrite {"local": "2026-11-25T11:00:00", "timezone": "America/Toronto", "utc": ...}"""
    moment = moment or {}
    if moment.get("local") and moment.get("timezone"):
        return datetime.fromisoformat(moment["local"]).replace(tzinfo=ZoneInfo(moment["timezone"]))
    return to_local(moment.get("utc") or moment.get("local"))


# ---------- 1. the API ----------

def auth_headers():
    token = os.environ.get("EVENTBRITE_TOKEN", "").strip()
    if not token:
        raise CollectorError("EVENTBRITE_TOKEN is not set")
    return {"Authorization": f"Bearer {token}"}


def fetch_api_events(http, headers, status):
    params = {
        "status": status,
        "order_by": "start_asc",
        "expand": "venue,ticket_availability,category,format",
    }
    events = []
    for page in range(1, MAX_PAGES + 1):
        data = http.json(f"{API}/organizers/{ORGANIZER_ID}/events/", params=params, headers=headers)
        events += data.get("events") or []
        pagination = data.get("pagination") or {}
        if not pagination.get("has_more_items"):
            break
        if pagination.get("continuation"):
            params["continuation"] = pagination["continuation"]
        else:
            params["page"] = page + 1
    return events


def full_description(http, headers, event_id):
    """Newer Eventbrite events keep only a summary in `description`; the full text is separate."""
    try:
        data = http.json(f"{API}/events/{event_id}/description/", headers=headers)
        return html_to_text(data.get("description"))
    except (requests.RequestException, ValueError) as exc:
        log.warning("edge: no full description for %s: %s", event_id, exc)
        return ""


def organizer_logo(http, headers):
    try:
        data = http.json(f"{API}/organizers/{ORGANIZER_ID}/", headers=headers)
        return (data.get("logo") or {}).get("url")
    except (requests.RequestException, ValueError) as exc:
        log.warning("edge: could not load organizer logo: %s", exc)
        return None


def venue_address(is_online, venue):
    if is_online:
        return {"venue": "Online", "lines": ["Online"], "map_url": None}
    venue = venue or {}
    place = venue.get("address") or {}
    street = place.get("localized_address_display") or ", ".join(
        part for part in [place.get("address_1"), place.get("city")] if part
    )
    name = (venue.get("name") or "").strip() or street or "Location not listed"
    lines = [line for line in dict.fromkeys([name, street]) if line and line != "Location not listed"]
    return {
        "venue": name,
        "lines": lines,
        "map_url": map_url(street or None, place.get("latitude") or venue.get("latitude"),
                           place.get("longitude") or venue.get("longitude")) if lines else None,
    }


def ticket_info(tickets, is_free):
    tickets = tickets or {}
    price = (tickets.get("minimum_ticket_price") or {}).get("display")
    if tickets.get("is_sold_out"):
        status = "Sold out"
    elif tickets.get("has_available_tickets") is False:
        status = "Tickets unavailable"
    else:
        status = None
    return ("Free" if is_free else price), status


def build_from_api(http, headers, event, fallback_image):
    tickets = event.get("ticket_availability") or {}
    is_free = bool(event.get("is_free") or tickets.get("is_free"))
    is_online = bool(event.get("online_event"))
    price, sales_status = ticket_info(tickets, is_free)
    logo = event.get("logo") or {}
    summary = (event.get("summary") or (event.get("description") or {}).get("text") or "").strip()

    return build_event(
        title=(event.get("name") or {}).get("text") or "",
        url=event["url"],
        start=local_time(event["start"]),
        end=local_time(event.get("end")),
        image_url=logo.get("url") or (logo.get("original") or {}).get("url") or fallback_image,
        description=full_description(http, headers, event["id"]) or summary,
        excerpt=summary or None,
        address=venue_address(is_online, event.get("venue")),
        categories=(["FREE"] if is_free else []) + (["Online"] if is_online else []),
        tags=[(event.get("category") or {}).get("name"), (event.get("format") or {}).get("name")],
        eventbrite_id=event["id"],
        is_online=is_online,
        is_cancelled=event.get("status") == "canceled",
        price=price,
        sales_status=sales_status,
    )


def collect_api(http):
    headers = auth_headers()
    found = {}
    for status in STATUSES:
        try:
            batch = fetch_api_events(http, headers, status)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code in (401, 403):
                raise CollectorError(f"Eventbrite rejected the token (HTTP {code})") from None
            if status != STATUSES[0] and code == 400:
                log.warning("edge: Eventbrite doesn't accept status=%s here; skipping it", status)
                continue
            raise
        for event in batch:
            found.setdefault(event["id"], event)
    log.info("edge: %d events from the Eventbrite API", len(found))

    fallback_image = organizer_logo(http, headers) if found else None
    events = []
    for event in found.values():
        if event.get("status") in ("canceled", "draft") or event.get("listed") is False:
            continue
        try:
            events.append(build_from_api(http, headers, event, fallback_image))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("edge: skipped event %s: %s", event.get("id"), exc)
    return events


# ---------- 2. the scraper fallback ----------

def page_json(soup, script_id=None):
    if script_id:
        script = soup.find("script", id=script_id)
        return json.loads(script.string) if script and script.string else None
    found = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            found.append(json.loads(script.string or ""))
        except ValueError:
            continue
    return found


def collect_page(http):
    soup = BeautifulSoup(http.text(ORGANIZER_URL), "html.parser")
    next_data = page_json(soup, "__NEXT_DATA__")
    if not next_data:
        raise CollectorError("organizer page has no __NEXT_DATA__ (page layout changed?)")
    props = next_data["props"]["pageProps"]
    if props.get("upcomingEventsFailed"):
        raise CollectorError("Eventbrite's organizer page says it failed to load upcoming events")
    summaries = props.get("upcomingEvents") or []
    if props.get("hasMoreUpcoming"):
        log.warning("edge: organizer page shows %d of %s upcoming events; the API is needed for the rest",
                    len(summaries), props.get("upcomingEventsTotal"))
    logo = next((d.get("mainEntity", {}).get("logo") for d in page_json(soup) if isinstance(d, dict)), None)
    log.info("edge: %d events from the organizer page", len(summaries))

    events = []
    for summary in summaries:
        if summary.get("is_cancelled"):
            continue
        try:
            events.append(build_from_page(http, summary, logo))
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            log.warning("edge: skipped %s: %s", summary.get("url"), exc)
    if summaries and not events:
        raise CollectorError("organizer page listed events but none could be read")
    return events


def build_from_page(http, summary, logo):
    zone = summary.get("timezone") or "America/Toronto"
    start = local_time({"local": f"{summary['start_date']}T{summary['start_time']}", "timezone": zone})
    end, venue, description = None, None, ""

    # Exact times, venue and description from the event page's ld+json
    event_page = BeautifulSoup(http.text(summary["url"]), "html.parser")
    for data in page_json(event_page):
        if isinstance(data, dict) and str(data.get("@type", "")).endswith("Event"):
            start = to_local(data.get("startDate")) or start
            end = to_local(data.get("endDate"))
            location = data.get("location") or {}
            if location.get("@type") == "Place":
                venue = {"name": location.get("name"),
                         "address": {"localized_address_display": (location.get("address") or {}).get("streetAddress")}}
            description = (data.get("description") or "").strip()
            break
    venue = summary.get("primary_venue") or venue

    is_online = bool(summary.get("is_online_event"))
    tickets = summary.get("ticket_availability") or {}
    is_free = bool(tickets.get("is_free"))
    price, sales_status = ticket_info(tickets, is_free)
    excerpt = (summary.get("summary") or "").strip()
    return build_event(
        title=summary["name"],
        url=summary["url"],
        start=start,
        end=end,
        image_url=(summary.get("image") or {}).get("url") or logo,
        description=description or excerpt,
        excerpt=excerpt or None,
        address=venue_address(is_online, venue),
        categories=(["FREE"] if is_free else []) + (["Online"] if is_online else []),
        eventbrite_id=summary.get("id"),
        is_online=is_online,
        is_cancelled=False,
        price=price,
        sales_status=sales_status or (summary.get("event_sales_status") or {}).get("message"),
    )


# ---------- entry point ----------

def collect(http):
    try:
        return collect_api(http)
    except Exception as exc:  # any API problem -> try the public page instead
        log.warning("edge: Eventbrite API unavailable (%s); falling back to the organizer page", exc)
    return collect_page(http)
