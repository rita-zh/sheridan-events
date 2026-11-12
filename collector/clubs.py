"""Sheridan club events from Campus Labs Engage's JSON search endpoint.

The /engage/events page is a React app that calls this endpoint itself:
  /engage/api/discovery/event/search?endsAfter=...&status=Approved&take=100&skip=0
It returns {"@odata.count": N, "value": [...]} and is paged with skip/take.
It's undocumented, so anything unexpected raises CollectorError and the runner
keeps the previous club data.

Image formats (checked against real responses, Nov 2026):
  imagePath "abc.png" -> https://se-images.campuslabs.ca/clink/images/abc.png?preset=med-w
  no imagePath        -> the theme's default, e.g. .../discovery/images/events/social.jpg
"""

import logging
import re

from .common import CollectorError, build_event, html_to_text, map_url, now_utc

log = logging.getLogger(__name__)

BASE_URL = "https://sheridancollege.campuslabs.ca"
SEARCH_URL = f"{BASE_URL}/engage/api/discovery/event/search"
IMAGE_URL = "https://se-images.campuslabs.ca/clink/images/{}?preset=med-w"
THEME_IMAGE_URL = "https://static.campuslabsengage.com/discovery/images/events/{}.jpg"
PAGE_SIZE = 100
MAX_PAGES = 30

# Engage theme code -> (label shown on the site, default image name)
THEMES = {
    "Arts": ("Arts", "artsandmusic"),
    "Athletics": ("Athletics", "athletics"),
    "CommunityService": ("Community Service", "service"),
    "Cultural": ("Cultural", "cultural"),
    "Fundraising": ("Fundraising", "fundraising"),
    "GroupBusiness": ("Group Business", "groupbusiness"),
    "Social": ("Social", "social"),
    "Spirituality": ("Spirituality", "spirituality"),
    "ThoughtfulLearning": ("Thoughtful Learning", "learning"),
}
ONLINE = re.compile(r"^https?://|\b(online|virtual|zoom|ms teams|microsoft teams|webinar)\b", re.I)


def fetch_all(http):
    ends_after = now_utc().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    items, skip = [], 0
    for _ in range(MAX_PAGES):
        data = http.json(SEARCH_URL, params={
            "endsAfter": ends_after,
            "status": "Approved",
            "orderByField": "endsOn",
            "orderByDirection": "ascending",
            "take": PAGE_SIZE,
            "skip": skip,
        })
        if not isinstance(data, dict) or not isinstance(data.get("value"), list):
            raise CollectorError(f"Engage search returned an unexpected shape: {str(data)[:200]}")
        batch = data["value"]
        items += batch
        skip += len(batch)
        total = data.get("@odata.count")
        if len(batch) < PAGE_SIZE or (isinstance(total, int) and skip >= total):
            break
    else:
        log.warning("clubs: stopped after %d pages; there may be more events", MAX_PAGES)
    return items


def build(item):
    theme_label, theme_image = THEMES.get(item.get("theme"), (item.get("theme"), None))
    location = (item.get("location") or "").strip()
    is_online = bool(location) and bool(ONLINE.search(location))

    if is_online:
        address = {"venue": "Online", "lines": ["Online"], "map_url": None}
    else:
        venue = location or "Location not listed"
        address = {
            "venue": venue,
            "lines": [location] if location else [],
            "map_url": map_url(location, item.get("latitude"), item.get("longitude")) if location else None,
        }

    if item.get("imagePath"):
        image_url = IMAGE_URL.format(item["imagePath"])
    elif theme_image:
        image_url = THEME_IMAGE_URL.format(theme_image)
    else:
        image_url = None

    hosts = [name.strip() for name in item.get("organizationNames") or [] if name and name.strip()]
    if not hosts and item.get("organizationName"):
        hosts = [item["organizationName"].strip()]
    categories = [c.strip() for c in item.get("categoryNames") or [] if c]
    benefits = [b.strip() for b in item.get("benefitNames") or [] if b]

    return build_event(
        title=item["name"],
        url=f"{BASE_URL}/engage/event/{item['id']}",
        start=item["startsOn"],
        end=item.get("endsOn"),
        image_url=image_url,
        description=html_to_text(item.get("description")),
        address=address,
        # Engage categories (Social, Workshop...) plus the event's theme
        categories=categories + ([theme_label] if theme_label else []) + (["Online"] if is_online else []),
        # Perks (Free Food...) plus the clubs running it
        tags=benefits + hosts,
        engage_id=str(item["id"]),
        organizations=hosts,
        rsvp_total=item.get("rsvpTotal"),
        is_online=is_online,
    )


def collect(http):
    items = fetch_all(http)
    log.info("clubs: %d events from the Engage API", len(items))
    events = []
    for item in items:
        if item.get("visibility") not in (None, "Public"):
            continue
        try:
            events.append(build(item))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("clubs: skipped event %s (%s): %s", item.get("id"), item.get("name"), exc)
    if items and not events:
        raise CollectorError("Engage returned events but none could be read; the format may have changed")
    return events
