"""Collect every source and write data/events.json.

    python collect.py                 # all sources
    python collect.py --only clubs    # just one (others keep their saved data)
    python collect.py --dry-run       # collect and report, don't write

Reliability rules:
  - Each source runs on its own. If it fails (or suddenly returns nothing
    while it still had upcoming events), its previous events are kept, its
    status is set to "error" and a clear error is logged.
  - Events are de-duplicated on (source, title, start date).
  - Events whose end date (or start date if no end) is in the past are dropped.

Exit code: 0 = all sources OK, 2 = some failed (data still written),
1 = every source failed.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import clubs, edge, radio, sheridan, ssu, theatre
from .common import CollectorError, finalize, is_upcoming, now_utc
from .http import Http

log = logging.getLogger("collector")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = Path(os.environ.get("EVENTS_FILE", REPO_ROOT / "data" / "events.json"))
# A copy published with the website (Vercel serves frontend/), so the page can
# show events instantly without waiting for the API to wake up.
SITE_COPY = REPO_ROOT / "frontend" / "events.json"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    url: str
    collect: Callable


# Same keys/names the API and front-end already use
SOURCES = [
    Source("ssu", "SSU", "https://www.thessu.ca/events", ssu.collect),
    Source("clubs", "Sheridan Clubs", "https://sheridancollege.campuslabs.ca/engage/events", clubs.collect),
    Source("edge", "EDGE", edge.ORGANIZER_URL, edge.collect),
    Source("sheridan", "Sheridan College", "https://www.sheridancollege.ca/events", sheridan.collect),
    Source("theatre", "Theatre Sheridan", theatre.WHATS_ON_URL, theatre.collect),
    Source("radio", "SheridanLife Radio", radio.EVENTS_URL, radio.collect),
]


# ---------- saved data ----------

def load_previous(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return data
        log.warning("%s has an unexpected shape; starting fresh", path)
    except FileNotFoundError:
        log.info("%s doesn't exist yet; starting fresh", path)
    except (OSError, ValueError) as exc:
        log.warning("couldn't read %s (%s); starting fresh", path, exc)
    return {"events": [], "sources": {}}


def write_atomically(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------- clean-up ----------

def dedupe_key(event):
    title = re.sub(r"\W+", " ", (event.get("title") or "").casefold()).strip()
    return (event["source"], title, event["start_date"])


def tidy(events):
    """Drop past events, de-duplicate, make ids unique, sort by start."""
    seen, kept = set(), []
    for event in events:
        if not is_upcoming(event):
            continue
        key = dedupe_key(event)
        if key in seen:
            log.info("dropped duplicate: %s %s (%s)", event["source"], event["title"], event["start_date"])
            continue
        seen.add(key)
        kept.append(event)

    # Two events can share a URL (e.g. two dates both linking to one page)
    counts = Counter(event["id"] for event in kept)
    for event in kept:
        if counts[event["id"]] > 1:
            event["id"] = f"{event['id']}-{event['start_date']}"

    return sorted(kept, key=lambda e: (e["start_date"], e.get("time_24h") or "", e["title"].casefold()))


# ---------- running ----------

def run_source(source, http, previous_events, previous_meta, started_at):
    """Returns (events to save for this source, status dict)."""
    previous_upcoming = [e for e in previous_events if is_upcoming(e)]
    meta = {
        "name": source.name,
        "source_url": source.url,
        "last_attempt": started_at,
        "last_success": previous_meta.get("last_success"),
    }
    clock = time.monotonic()
    try:
        records = source.collect(http)
        events = [e for e in (finalize(r, source) for r in records) if is_upcoming(e)]
        if not events and previous_upcoming:
            raise CollectorError(
                f"returned 0 upcoming events but {len(previous_upcoming)} were saved before; "
                "treating as a failure in case the site changed"
            )
    except Exception as exc:  # one source must never break the run
        message = f"{type(exc).__name__}: {exc}"
        log.error("[%s] FAILED: %s; keeping %d previously saved upcoming events",
                  source.key, message, len(previous_upcoming))
        log.debug("%s", traceback.format_exc())
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error title={source.name} collector failed::{message}", flush=True)
        return previous_upcoming, {**meta, "status": "error", "error": message[:500],
                                   "count": len(previous_upcoming)}

    log.info("[%s] OK: %d upcoming events in %.1fs", source.key, len(events), time.monotonic() - clock)
    return events, {**meta, "status": "ok", "error": None, "last_success": started_at, "count": len(events)}


def step_summary(sources_meta, path):
    lines = ["| Source | Status | Upcoming events | Last success |", "|---|---|---|---|"]
    for meta in sources_meta.values():
        status = "✅ ok" if meta["status"] == "ok" else f"❌ {meta['error']}"
        lines.append(f"| {meta['name']} | {status} | {meta['count']} | {meta['last_success'] or 'never'} |")
    table = "\n".join(lines)
    print("\n" + table + "\n")
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(f"## Event collection → `{path.name}`\n\n{table}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", choices=[s.key for s in SOURCES], help="collect just these sources")
    parser.add_argument("--output", type=Path, default=DATA_FILE, help=f"default: {DATA_FILE}")
    parser.add_argument("--dry-run", action="store_true", help="don't write the file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    previous = load_previous(args.output)
    previous_meta = previous.get("sources") or {}
    started_at = now_utc().isoformat(timespec="seconds")
    http = Http()

    all_events, sources_meta, failed = [], {}, []
    for source in SOURCES:
        previous_events = [e for e in previous["events"] if e.get("source") == source.key]
        if args.only and source.key not in args.only:
            # Not run this time: carry over as-is
            kept = [e for e in previous_events if is_upcoming(e)]
            all_events += kept
            sources_meta[source.key] = {**previous_meta.get(source.key, {"name": source.name, "source_url": source.url,
                                        "status": "skipped", "error": None, "last_success": None,
                                        "last_attempt": None}), "count": len(kept)}
            continue
        events, meta = run_source(source, http, previous_events, previous_meta.get(source.key, {}), started_at)
        all_events += events
        sources_meta[source.key] = meta
        if meta["status"] == "error":
            failed.append(source.key)

    events = tidy(all_events)
    for meta_key, meta in sources_meta.items():
        meta["count"] = sum(1 for e in events if e["source"] == meta_key)

    successes = [m["last_success"] for m in sources_meta.values() if m.get("last_success")]
    output = {
        "schema_version": SCHEMA_VERSION,
        # When the newest data was fetched: the latest successful source run
        "last_updated": max(successes) if successes else None,
        "generated_at": started_at,
        "sources": sources_meta,
        "count": len(events),
        "events": events,
    }

    if args.dry_run:
        log.info("dry run: not writing %s", args.output)
    else:
        write_atomically(args.output, output)
        if args.output == DATA_FILE:
            write_atomically(SITE_COPY, output)
        log.info("wrote %d events to %s (%d HTTP requests)", len(events), args.output, http.requests_made)
    step_summary(sources_meta, args.output)

    ran = [s.key for s in SOURCES if not args.only or s.key in args.only]
    if failed and len(failed) == len(ran):
        return 1
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
