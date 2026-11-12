"""SSU events from thessu.ca (a Squarespace site).

The /events page shows a summary of the "ssu-events" calendar collection.
Squarespace serves that collection as JSON, so no HTML scraping is needed:

  1. /ssu-events?format=json -> "upcoming": every upcoming event with dates
     (epoch ms), location, image, categories, tags and the full body.
  2. /api/open/GetItemsByMonth?month=MM-YYYY&collectionId=... -> what the
     calendar view loads, month by month. Used as a cross-check for the next
     few months in case "upcoming" is ever truncated.

If the JSON stops working, it falls back to reading the HTML pages.
"""

import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .common import (
    CollectorError, add_months, build_event, clean, html_to_text, map_url, now_utc, to_local, today_local,
)

log = logging.getLogger(__name__)

SITE = "https://www.thessu.ca"
LIST_URL = f"{SITE}/events"
COLLECTION_URL = f"{SITE}/ssu-events"
MONTHS_AHEAD = 6


# ---------- JSON (preferred) ----------

def item_dates(item):
    content = item.get("structuredContent") or {}
    start = item.get("startDate") or content.get("startDate")
    end = item.get("endDate") or content.get("endDate") or start
    return start, end


def fetch_json_items(http):
    data = http.json(COLLECTION_URL, params={"format": "json"})
    upcoming = data.get("upcoming")
    if not isinstance(upcoming, list):
        raise CollectorError("Squarespace JSON has no 'upcoming' list")
    items = {item["fullUrl"]: item for item in upcoming if item.get("fullUrl")}

    collection_id = (data.get("collection") or {}).get("id")
    now_ms = now_utc().timestamp() * 1000
    first_month = today_local().replace(day=1)
    for offset in range(MONTHS_AHEAD + 1):
        month = add_months(first_month, offset)
        if not collection_id:
            break
        try:
            month_items = http.json(f"{SITE}/api/open/GetItemsByMonth", params={
                "month": f"{month:%m-%Y}", "collectionId": collection_id,
            })
        except (requests.RequestException, ValueError) as exc:
            log.warning("ssu: GetItemsByMonth %s failed (%s); relying on the upcoming list", f"{month:%m-%Y}", exc)
            continue
        for item in month_items if isinstance(month_items, list) else []:
            start, end = item_dates(item)
            if item.get("fullUrl") and item["fullUrl"] not in items and end and end >= now_ms:
                log.info("ssu: %s only found via GetItemsByMonth", item["fullUrl"])
                items[item["fullUrl"]] = item
    return list(items.values())


def build_from_json(item):
    start, end = item_dates(item)
    if not start:
        raise ValueError("no start date")
    url = urljoin(SITE, item["fullUrl"])

    location = item.get("location") or {}
    title_line = clean(location.get("addressTitle") or "")
    lines = [clean(line) for line in [title_line, location.get("addressLine1"), location.get("addressLine2")] if line and clean(line)]
    has_place = bool(lines)
    address = {
        "venue": title_line or (lines[0] if lines else "Location not listed"),
        "lines": lines,
        "map_url": map_url(", ".join(lines[1:]) or None, location.get("markerLat"), location.get("markerLng")) if has_place else None,
    }

    image = item.get("assetUrl")
    if image and "squarespace-cdn.com" in image and "?" not in image:
        image += "?format=750w"  # Squarespace resizes on the fly; originals can be 2000px+

    excerpt = html_to_text(item.get("excerpt")).replace("\n", " ")
    description = html_to_text(item.get("body")) or excerpt
    return build_event(
        title=item.get("title") or "",
        url=url,
        start=start,
        end=end,
        image_url=image,
        description=description,
        excerpt=excerpt or None,
        address=address,
        categories=item.get("categories") or [],
        tags=item.get("tags") or [],
        ics_url=f"{url}?format=ical",
    )


# ---------- HTML (fallback) ----------

def event_links(http):
    links = []
    for page in (COLLECTION_URL, LIST_URL):
        try:
            soup = BeautifulSoup(http.text(page), "html.parser")
        except requests.RequestException as exc:
            log.warning("ssu: could not load %s: %s", page, exc)
            continue
        for a in soup.select('a[href*="/ssu-events/"]'):
            url = urljoin(SITE, a.get("href", "")).split("?")[0].rstrip("/")
            if urlparse(url).netloc.endswith("thessu.ca") and url.count("/") > 3:
                links.append(url)
    return list(dict.fromkeys(links))


def build_from_html(http, url):
    soup = BeautifulSoup(http.text(url), "html.parser")
    day = soup.select_one("time.event-date[datetime]")
    if not day:
        raise ValueError("no event date on the page")

    # Squarespace renders <time datetime="2026-12-10">16:00</time> for 24h start/end
    # (the end tag's class is sometimes mislabelled 12hr, so accept either)
    def moment(elements):
        for el in elements:
            match = re.fullmatch(r"(\d{1,2}):(\d{2})", el.get_text(strip=True))
            if match and el.get("datetime"):
                return to_local(f"{el['datetime']}T{int(match[1]):02d}:{match[2]}")
        return None

    start = moment(soup.select("time.event-time-24hr-start"))
    time_known = start is not None
    start = start or to_local(f"{day['datetime']}T00:00")
    end = moment(soup.select("time.event-time-24hr-end, time.event-time-12hr-end")) or start

    lines = [clean(el.get_text(" ", strip=True)) for el in soup.select(".eventitem-meta-address-line")]
    lines = [line for line in lines if line and line.lower() != "canada"]
    content = soup.select_one(".eventitem-column-content")
    description = html_to_text(str(content)) if content else ""
    og_image = soup.find("meta", property="og:image")
    title = soup.select_one("h1.eventitem-title, h1")
    return build_event(
        title=title.get_text(" ", strip=True) if title else slugify_title(url),
        url=url,
        start=start,
        end=end,
        time_known=time_known,
        image_url=og_image["content"] if og_image and og_image.get("content") else None,
        description=description,
        address={
            "venue": lines[0] if lines else "Location not listed",
            "lines": lines,
            "map_url": map_url(", ".join(lines[1:]) or None) if lines else None,
        },
        ics_url=f"{url}?format=ical",
    )


def slugify_title(url):
    return url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()


def collect_html(http):
    events = []
    for url in event_links(http):
        try:
            events.append(build_from_html(http, url))
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("ssu: skipped %s: %s", url, exc)
    return events


# ---------- entry point ----------

def collect(http):
    try:
        items = fetch_json_items(http)
    except (requests.RequestException, ValueError, KeyError, CollectorError) as exc:
        log.warning("ssu: JSON feed failed (%s); falling back to HTML", exc)
        return collect_html(http)

    log.info("ssu: %d events from the Squarespace JSON", len(items))
    events = []
    for item in items:
        try:
            events.append(build_from_json(item))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("ssu: skipped %s: %s", item.get("fullUrl"), exc)
    if items and not events:
        raise CollectorError("Squarespace returned events but none could be read")
    return events
