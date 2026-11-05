"""Scrape Sheridan Student Union events into the existing events JSON schema.

From the events-sheridan project root:
    python -m backend.pyfiles.scrape_ssu

Requires selenium, beautifulsoup4, and Chrome. Writes ssuevents.json to the
current directory; inspect before moving it to backend/api/ssuevents.json.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from selenium.webdriver.support.ui import WebDriverWait

from backend.pyfiles.scrape_edge import make_driver
from backend.pyfiles.normalize_events import normalize_file

URL = "https://www.thessu.ca/events"
OUTPUT = Path("ssuevents.json")
TZ = ZoneInfo("America/Toronto")


def text(el):
    return el.get_text(" ", strip=True) if el else ""


def load(driver, url):
    driver.get(url)
    WebDriverWait(driver, 25).until(lambda d: d.execute_script("return document.readyState") == "complete")
    return BeautifulSoup(driver.page_source, "html.parser")


def event_links(soup):
    """Read Squarespace event summary cards; exclude duplicate links."""
    found = {}
    for a in soup.select('a[href*="/ssu-events/"]'):
        url = urljoin(URL, a.get("href", "")).split("?")[0].rstrip("/")
        if urlparse(url).netloc not in ("www.thessu.ca", "thessu.ca"):
            continue
        if not urlparse(url).path.startswith("/ssu-events/"):
            continue
        card = a.find_parent(class_=re.compile(r"summary-item|eventlist-event", re.I))
        if not card:
            card = a.parent
        title = text(card.select_one('.summary-title-link, .summary-title, .eventlist-title')) or text(a)
        excerpt = text(card.select_one('.summary-excerpt, .eventlist-excerpt'))
        img = card.select_one('img[data-src], img[src]')
        image = (img.get('data-src') or img.get('src')) if img else None
        if image:
            image = urljoin(URL, image)
        if not title or title.lower() in ('read more', 'read more →'):
            continue
        # Prefer cards with excerpt/images when duplicate links occur.
        if url not in found or (excerpt and not found[url]['excerpt']):
            found[url] = dict(title=title, excerpt=excerpt, image_url=image)
    return found


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def dates_from_page(soup):
    # Squarespace event pages typically expose start/end in time[datetime].
    times = []
    for el in soup.select('time[datetime], [itemprop="startDate"][content], [itemprop="endDate"][content]'):
        dt = parse_iso(el.get('datetime') or el.get('content'))
        if dt and dt.year >= 2025:
            times.append(dt)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for item in values:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get('@graph'), list):
                values.extend(item['@graph'])
            kind = item.get('@type', '')
            if isinstance(kind, list):
                kind = ' '.join(kind)
            if 'Event' in kind:
                start = parse_iso(item.get('startDate'))
                end = parse_iso(item.get('endDate'))
                if start:
                    return start, end or start
    if times:
        return times[0], times[1] if len(times) > 1 else times[0]
    return None, None


def scrape_detail(driver, url, fallback):
    soup = load(driver, url)
    title = text(soup.select_one('h1.eventitem-title, h1')) or fallback['title']
    start, end = dates_from_page(soup)
    if not start:
        # Squarespace also renders event dates as human-readable text.
        date_text = text(soup.select_one('.eventitem-meta-date, .event-meta-date'))
        time_text = text(soup.select_one('.eventitem-meta-time, .event-meta-time'))
        match = re.search(r'(\w+\s+\d{1,2},?\s+20\d{2})', date_text)
        if match:
            try:
                date = datetime.strptime(match.group(1).replace(',', ''), '%B %d %Y')
                clock = re.search(r'(\d{1,2}:\d{2})\s*([AP]M)', time_text, re.I)
                if clock:
                    t = datetime.strptime(clock.group(0), '%I:%M %p')
                    date = date.replace(hour=t.hour, minute=t.minute)
                start = date.replace(tzinfo=TZ)
            except ValueError:
                pass
    if not start:
        raise ValueError('No machine-readable or recognizable event date')

    content = soup.select_one('.eventitem-column-content, .eventitem-content, article')
    description = content.get_text('\n', strip=True) if content else fallback['excerpt']
    venue = text(soup.select_one('.eventitem-meta-address-title, .eventitem-meta-address, [itemprop="location"]'))
    if not venue:
        venue = 'Location not listed'
    image_el = soup.select_one('.eventitem-main-content img[data-src], .eventitem-main-content img[src], article img[data-src], article img[src]')
    image = fallback['image_url'] or ((image_el.get('data-src') or image_el.get('src')) if image_el else None)
    if image:
        image = urljoin(URL, image)

    return {
        'title': title, 'url': url, 'image_url': image,
        'list_date_box': {'month': start.strftime('%b'), 'day': str(start.day)},
        'excerpt': fallback['excerpt'] or description[:250],
        'last_occurrence_ends_at': (end or start).astimezone(timezone.utc).isoformat(),
        'occurrences': [{
            'date': start.strftime('%Y-%m-%d'),
            'date_text': start.strftime('%A, %B %d, %Y'),
            'time_12h': start.strftime('%-I:%M %p'),
            'time_24h': start.strftime('%H:%M'),
        }],
        'address': {'venue': venue, 'lines': [venue], 'map_url': None},
        'categories': [], 'tags': [],
        'google_calendar_url': None,
        'ics_url': url + '?format=ical',
        'description': description,
    }


def main():
    driver = make_driver()
    try:
        cards = event_links(load(driver, URL))
        print(f'Found {len(cards)} possible SSU events')
        if not cards:
            raise RuntimeError('Found no SSU event links; keeping existing JSON unchanged')
        events = []
        for url, summary in cards.items():
            try:
                event = scrape_detail(driver, url, summary)
                events.append(event)
                print(f"  scraped: {event['title']}")
            except Exception as exc:
                print(f'  skipped {url}: {exc}')
    finally:
        driver.quit()
    if not events:
        raise RuntimeError('No events parsed; keeping existing JSON unchanged')
    # Write directly to a separate candidate file for safe inspection.
    output = {
        'source': URL,
        'scraped_at': datetime.now(timezone.utc).isoformat(),
        'count': len(events), 'events': events,
    }
    OUTPUT.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding='utf-8')
    normalize_file(str(OUTPUT))
    print(f'Saved {len(events)} SSU events to {OUTPUT}')


if __name__ == '__main__':
    main()
