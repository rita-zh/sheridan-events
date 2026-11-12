"""Sheridan College events from sheridancollege.ca/events (Sitecore SXA).

The /events page loads its list with JavaScript from a JSON endpoint:
  /sxa/search/results/?s={scope}&itemid={page}&v={view}&p=20&e=<offset>
  -> {"Count": total, "Results": [{"Url": ..., "Html": "<card markup>"}]}
Paging is the e (offset) parameter. The scope/page/view ids are read from the
page's data-properties so it keeps working if they change.

Categories aren't on the cards; they're facets. Asking the same endpoint with
&event category=<name> returns only that category's events.

Each event's own page has the description (.event-content) and sometimes an
image; some links (e.g. Open House) go to a regular page instead.
"""

import json
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .common import LOCAL_TZ, CollectorError, build_event, clean, html_to_text, map_url, parse_day

log = logging.getLogger(__name__)

SITE = "https://www.sheridancollege.ca"
EVENTS_URL = f"{SITE}/events"
RESULTS_URL = f"{SITE}/sxa/search/results/"
FACETS_URL = f"{SITE}/sxa/search/facets/"
# Values seen on the page in Nov 2026, used if they can't be read from the page
DEFAULT_SEARCH = {
    "s": "{43499FEF-671B-4022-98F9-D5C0D019EEDB}",
    "itemid": "{D6B43D0D-2EFA-4066-AA52-233F81D7B989}",
    "v": "{9B8939E0-41F7-4F09-972B-F4B735193D35}",
}
PAGE_SIZE = 20
MAX_PAGES = 25
CAMPUSES = ["Davis Campus", "Trafalgar Campus", "Hazel McCallion Campus"]
TIME_PATTERN = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?", re.I)


# ---------- the search endpoint ----------

def search_config(http):
    try:
        soup = BeautifulSoup(http.text(EVENTS_URL), "html.parser")
        # The page also has site-wide search boxes; the event list is .search-results--event
        el = soup.select_one(".search-results--event[data-properties]")
        if el:
            props = json.loads(el["data-properties"])
            if all(props.get(key) for key in DEFAULT_SEARCH):
                return {key: props[key] for key in DEFAULT_SEARCH}
        log.warning("sheridan: event list settings not found on the page; using defaults")
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning("sheridan: couldn't read search settings from the page (%s); using defaults", exc)
    return dict(DEFAULT_SEARCH)


def fetch_results(http, config, extra=None):
    results, offset, total = [], 0, None
    for _ in range(MAX_PAGES):
        data = http.json(RESULTS_URL, params={**config, "p": PAGE_SIZE, "e": offset, **(extra or {})})
        if not isinstance(data, dict) or not isinstance(data.get("Results"), list):
            raise CollectorError(f"SXA search returned an unexpected shape: {str(data)[:200]}")
        batch = data["Results"]
        total = data.get("Count", total)
        results += batch
        offset += len(batch)
        if not batch or (isinstance(total, int) and offset >= total):
            break
    if isinstance(total, int) and len(results) < total:
        log.warning("sheridan: got %d of %d results", len(results), total)
    return results


def fetch_categories(http, config):
    """result Url -> list of category names. Best effort: errors only lose categories."""
    categories = {}
    try:
        facets = http.json(FACETS_URL, params={**config, "f": "event category"}).get("Facets") or []
        names = [value["Name"] for facet in facets for value in facet.get("Values") or [] if value.get("Name")]
        for name in names:
            for result in fetch_results(http, config, {"event category": name}):
                categories.setdefault(result.get("Url"), []).append(name)
    except (requests.RequestException, ValueError, KeyError, CollectorError) as exc:
        log.warning("sheridan: categories unavailable this run: %s", exc)
    return categories


# ---------- cards and detail pages ----------

def parse_card(result):
    soup = BeautifulSoup(result.get("Html") or "", "html.parser")
    link = soup.select_one("h3 a[href]")
    if not link:
        return None
    dates = [clean(s.get_text()) for s in soup.select(".search-result-event__date span") if re.search(r"\d{4}", s.get_text())]
    times = soup.select_one(".search-result-event__time")
    location = soup.select_one(".search-result-event__location")
    location_text = clean(location.get_text(" ", strip=True)) if location else ""
    return {
        "key": result.get("Url"),
        "url": urljoin(SITE, link["href"]),
        "title": clean(link.get_text(" ", strip=True)),
        "dates": dates,
        "time_text": clean(times.get_text(" ", strip=True)) if times else "",
        "location": re.sub(r"\s+,", ",", re.sub(r"^Location:\s*", "", location_text)).strip(),
    }


def fetch_details(http, url):
    try:
        soup = BeautifulSoup(http.text(url), "html.parser")
    except requests.RequestException as exc:
        log.warning("sheridan: no details for %s: %s", url, exc)
        return {"description": "", "image_url": None, "page_text": ""}

    content = soup.select_one(".event-content")
    meta_description = soup.find("meta", attrs={"name": "description"})
    image_url = None
    if content:
        description = html_to_text(str(content))
        image = content.find("img", src=True)
        image_url = urljoin(SITE, image["src"]) if image else None
    else:
        description = clean(meta_description.get("content", "")) if meta_description else ""
    og_image = soup.find("meta", property="og:image")
    if not image_url and og_image and og_image.get("content"):
        image_url = urljoin(SITE, og_image["content"])
    if not image_url:
        banner = soup.select_one(".hero-banner img[src]")
        image_url = urljoin(SITE, banner["src"]) if banner else None

    main = soup.find("main") or soup.body or soup
    for tag in main(["script", "style", "noscript"]):
        tag.decompose()
    return {"description": description, "image_url": image_url, "page_text": main.get_text(" ", strip=True)}


def parse_times(text):
    """'3:00 p.m. – 4:00 p.m.' or '10 a.m.–2 p.m.' -> ['15:00', '16:00']"""
    times = []
    for hour, minute, half in TIME_PATTERN.findall(text or ""):
        hour = int(hour) % 12 + (12 if half.lower() == "p" else 0)
        times.append(f"{hour:02d}:{minute or '00'}")
    return times


def at(day, hhmm):
    hour, minute = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LOCAL_TZ)


def build(card, details, categories):
    days = [parse_day(text) for text in card["dates"]]
    if not days:
        raise ValueError("no date on the card")
    start_day, end_day = days[0], days[-1]

    # Time from the card, or from the detail page text when the card has none
    times = parse_times(card["time_text"]) or parse_times(details["page_text"])[:2]
    start = at(start_day, times[0] if times else "00:00")
    end = at(end_day, times[1] if len(times) > 1 else "23:59")

    campuses = [c for c in CAMPUSES if c in details["page_text"]] if not card["location"] else []
    place = card["location"] or ", ".join(campuses) or "Sheridan College"
    is_online = place.lower() == "online"
    if is_online:
        address = {"venue": "Online", "lines": ["Online"], "map_url": None}
    elif campuses:
        address = {"venue": place, "lines": campuses, "map_url": map_url(place)}
    else:
        address = {"venue": place.split(",")[0].strip(), "lines": [p.strip() for p in place.split(",")], "map_url": map_url(place)}

    return build_event(
        title=card["title"],
        url=card["url"],
        start=start,
        end=end,
        time_known=bool(times),
        extra_days=[end_day],
        image_url=details["image_url"],
        description=details["description"],
        address=address,
        categories=categories + (["Online"] if is_online else []),
        # Where it happens: "Online", or the campus names
        tags=["Online"] if is_online else campuses or [address["venue"]],
        is_online=is_online,
    )


def collect(http):
    config = search_config(http)
    results = fetch_results(http, config)
    cards = [card for card in map(parse_card, results) if card]
    log.info("sheridan: %d events from the SXA search", len(cards))
    if results and not cards:
        raise CollectorError(f"SXA returned {len(results)} results but no event cards; the markup may have changed")
    categories = fetch_categories(http, config) if cards else {}

    events = []
    for card in cards:
        try:
            details = fetch_details(http, card["url"])
            events.append(build(card, details, categories.get(card["key"], [])))
        except (KeyError, ValueError) as exc:
            log.warning("sheridan: skipped %s: %s", card["url"], exc)
    if cards and not events:
        raise CollectorError("SXA returned events but none could be read; the card markup may have changed")
    return events
