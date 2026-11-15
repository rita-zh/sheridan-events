"""Theatre Sheridan shows from sheridancollege.ca/theatre/whats-on.

The What's on page is plain HTML: one card per show with the title (linking to
the show's page), a date range like "December 9–18, 2026", the venue and a
"Buy tickets" link to TixHub. Each show's own page has the poster and the
description. Ticket prices come from the Box Office page.

Shows run over several days and the exact performance times are only on
TixHub, whose robots.txt asks crawlers to stay out of the ticket pages, so a
show is one event spanning its run, with the time left unknown.
"""

import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .common import LOCAL_TZ, CollectorError, build_event, clean, html_to_text, map_url, shorten

log = logging.getLogger(__name__)

SITE = "https://www.sheridancollege.ca"
WHATS_ON_URL = f"{SITE}/theatre/whats-on"
TICKETS_URL = f"{SITE}/theatre/tickets"
# Both theatres (Macdonald Heaslip Hall and the Studio Theatre) are here
CAMPUS = "Trafalgar Campus"
ADDRESS = "1430 Trafalgar Road, Oakville, ON"

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
# "December 9–18, 2026" / "March 27–April 10, 2027" / "Dec 31, 2026 – Jan 5, 2027" / "May 2, 2027"
RANGE = re.compile(
    r"(?P<m1>[A-Za-z]{3,9})\.?\s+(?P<d1>\d{1,2})(?:,\s*(?P<y1>\d{4}))?"
    r"(?:\s*[–—-]\s*(?:(?P<m2>[A-Za-z]{3,9})\.?\s+)?(?P<d2>\d{1,2}))?"
    r",\s*(?P<y2>\d{4})"
)


def month_number(name):
    number = MONTHS.get(name[:3].lower())
    if not number:
        raise ValueError(f"not a month: {name}")
    return number


def parse_range(text):
    """Date-range text -> (first day, last day)."""
    match = RANGE.search(clean(text) or "")
    if not match:
        raise ValueError(f"no date range in {text!r}")
    m1 = month_number(match["m1"])
    m2 = month_number(match["m2"]) if match["m2"] else m1
    y2 = int(match["y2"])
    y1 = int(match["y1"]) if match["y1"] else (y2 - 1 if m1 > m2 else y2)
    first = date(y1, m1, int(match["d1"]))
    last = date(y2, m2, int(match["d2"] or match["d1"]))
    return first, max(first, last)


# ---------- pages ----------

def largest_image(img):
    """The widest image in a srcset, else src."""
    if not img:
        return None
    best, best_width = img.get("src"), 0
    for part in (img.get("srcset") or "").split(","):
        bits = part.strip().rsplit(" ", 1)
        if len(bits) == 2 and bits[1].endswith("w") and bits[1][:-1].isdigit() and int(bits[1][:-1]) > best_width:
            best, best_width = bits[0].strip(), int(bits[1][:-1])
    return urljoin(SITE, best.replace("w=+", "w=")) if best else None


def parse_cards(markup):
    soup = BeautifulSoup(markup, "html.parser")
    main = soup.find("main") or soup
    cards = []
    for title_link in main.select(".widget-title a[href*='/theatre/whats-on/']"):
        card = title_link.find_parent(class_="widget") or title_link.parent
        body = card.select_one(".field-content")
        when = body.find("strong") if body else None
        venue = body.find("em") if body else None
        tickets = card.find("a", href=re.compile(r"tixhub\.com"))
        cards.append({
            "url": urljoin(SITE, title_link["href"]),
            "title": clean(title_link.get_text(" ", strip=True)),
            "when": clean((when or body or card).get_text(" ", strip=True)),
            "venue": clean(venue.get_text(" ", strip=True)) if venue else "Theatre Sheridan",
            "image_url": largest_image(card.find("img")),
            "tickets_url": tickets["href"] if tickets else TICKETS_URL,
        })
    return cards


CREDITS = re.compile(r"^(?:Presented\b|.{0,40}\b(?:by|Director|Choreographer|Designer)\b)", re.I)


def tidy_description(text):
    """Story first, credits ("Book by…", "Director: …") after; fix "Company , remains"."""
    lines = [re.sub(r"\s+([,.;:!?])", r"\1", line) for line in text.split("\n") if line.strip()]
    credits = [line for line in lines if CREDITS.match(line)]
    story = [line for line in lines if line not in credits]
    return "\n".join(story + credits)


def fetch_details(http, url):
    """Description and poster from the show's own page. Best effort."""
    try:
        soup = BeautifulSoup(http.text(url), "html.parser")
    except requests.RequestException as exc:
        log.warning("theatre: no details for %s: %s", url, exc)
        return {"description": "", "image_url": None}
    main = soup.find("main") or soup
    # The show's text is the longest rich-text block; the blue one is the
    # program blurb shared by every show, and the first just repeats date + venue
    blocks = [b for b in main.select(".rich-text") if "highlight-blue" not in (b.get("class") or [])]
    blocks = [b for b in blocks if "instagram" not in b.get_text().lower()]
    best = max(blocks, key=lambda b: len(b.get_text(strip=True)), default=None)
    description = tidy_description(html_to_text(str(best))) if best else ""
    if not description:
        meta = soup.find("meta", attrs={"name": "description"})
        description = clean(meta.get("content", "")) if meta else ""
    og_image = soup.find("meta", property="og:image")
    image_url = urljoin(SITE, og_image["content"]) if og_image and og_image.get("content") else None
    return {"description": description, "image_url": image_url}


def fetch_price(http):
    """'$18–$33 + fees' from the Box Office price table. None if it can't be read."""
    try:
        text = BeautifulSoup(http.text(TICKETS_URL), "html.parser").get_text(" ", strip=True)
    except requests.RequestException as exc:
        log.warning("theatre: ticket prices unavailable: %s", exc)
        return None
    # Rows look like "Adult (18–64) $33 $36.50": base price, then total with fees
    bases = [float(base) for base, _ in re.findall(r"\$(\d+(?:\.\d\d)?)\s+\$(\d+(?:\.\d\d)?)", text)]
    if not bases:
        return None
    low, high = min(bases), max(bases)
    money = lambda v: f"${v:g}" if v == int(v) else f"${v:.2f}"
    return f"{money(low)}–{money(high)} + fees" if low != high else f"{money(low)} + fees"


def build(card, details, price):
    first, last = parse_range(card["when"])
    start = datetime(first.year, first.month, first.day, tzinfo=LOCAL_TZ)
    end = datetime(last.year, last.month, last.day, 23, 59, tzinfo=LOCAL_TZ)
    venue = card["venue"]
    return build_event(
        title=card["title"],
        url=card["url"],
        start=start,
        end=end,
        time_known=False,
        extra_days=[last],
        image_url=details["image_url"] or card["image_url"],
        description=details["description"],
        # Skip the short tagline ("Normal is overrated.") and preview the first real paragraph
        excerpt=shorten(next((line for line in details["description"].split("\n") if len(line) > 80), "")) or None,
        address={"venue": venue, "lines": [venue, CAMPUS, ADDRESS], "map_url": map_url(f"{venue}, {ADDRESS}")},
        categories=["Arts", "Theatre"],
        tags=[CAMPUS],
        price=price,
        tickets_url=card["tickets_url"],
        is_online=False,
    )


def collect(http):
    cards = parse_cards(http.text(WHATS_ON_URL))
    log.info("theatre: %d shows on What's on", len(cards))
    if not cards:
        raise CollectorError("no show cards on the What's on page; the markup may have changed")
    price = fetch_price(http)

    events = []
    for card in cards:
        try:
            first, last = parse_range(card["when"])
        except ValueError as exc:
            log.warning("theatre: skipped %s: %s", card["url"], exc)
            continue
        if last < datetime.now(LOCAL_TZ).date():
            continue  # already closed; no need to fetch its page
        events.append(build(card, fetch_details(http, card["url"]), price))
    return events
