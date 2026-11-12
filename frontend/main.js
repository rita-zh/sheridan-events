const API_BASE = "https://sheridan-events.onrender.com";

const VIEW_ENDPOINTS = {
  all: () => "/api/events",
  today: () => "/api/events/today",
  week: () => "/api/events/week",
  month: () => `/api/events/month/${localDateString(new Date()).slice(0, 7)}`,
};

const VIEW_NAMES = {
  all: "All events",
  today: "Today",
  week: "This week",
  month: "This month",
};

const $ = (id) => document.getElementById(id);

const state = {
  view: "all",
  results: [],
  eventsById: new Map(),
  lastFocus: null,
};

// ---------- talking to the API ----------

async function fetchJson(path) {
  const response = await fetch(API_BASE + path);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

// e.g. /api/events/week?source=clubs&location=hmc&time=evening
function buildEventsUrl() {
  const params = new URLSearchParams();
  if ($("organizer").value) params.set("source", $("organizer").value);
  if ($("campus").value) params.set("location", $("campus").value);
  if ($("time").value) params.set("time", $("time").value);

  const query = params.toString();
  return VIEW_ENDPOINTS[state.view]() + (query ? `?${query}` : "");
}

// ---------- dates ----------

function localDateString(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(
    date.getDate()
  )}`;
}

// "2026-10-13" -> Date (noon, so time zones can't shift the day)
function toDate(dateString) {
  return new Date(`${dateString}T12:00:00`);
}

function format(dateString, options) {
  return dateString
    ? toDate(dateString).toLocaleDateString("en-CA", options)
    : "";
}

// "Tue, Oct 13"
function mediumDate(dateString) {
  return format(dateString, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

// "Tuesday, October 13, 2026"
function longDate(dateString) {
  return format(dateString, {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
  });
}

// "Tue, Oct 13" or "Tue, Oct 13 – Thu, Dec 10"
function dateRange(event) {
  if (event.end_date && event.end_date !== event.start_date) {
    return `${mediumDate(event.start_date)} – ${mediumDate(event.end_date)}`;
  }
  return mediumDate(event.start_date);
}

function isHappeningNow(event) {
  const today = localDateString(new Date());
  return event.start_date < today && event.end_date >= today;
}

// "4:00 PM", or the end time too when the calendar link has it: "4:00 PM – 8:00 PM"
function timeRange(event) {
  if (!event.time) return "Time not listed";
  const match = (event.google_calendar_url || "").match(
    /dates=(\d{8}T\d{6}Z)\/(\d{8}T\d{6}Z)/
  );
  if (!match) return event.time;
  const parse = (s) =>
    new Date(
      `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}T${s.slice(
        9,
        11
      )}:${s.slice(11, 13)}:00Z`
    );
  const start = parse(match[1]);
  const end = parse(match[2]);
  // Only show an end time for same-day events
  if (localDateString(start) !== localDateString(end)) return event.time;
  const endText = end
    .toLocaleTimeString("en-CA", { hour: "numeric", minute: "2-digit" })
    .replace(/\s?a\.m\./i, " AM")
    .replace(/\s?p\.m\./i, " PM");
  return `${event.time} – ${endText}`;
}

// ---------- small building blocks ----------

// Scraped text goes through this so it can't inject HTML into the page
function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

function colorClass(index) {
  return `c${(index % 6) + 1}`;
}

const NEW_TAB = '<span class="visually-hidden"> (opens in a new tab)</span>';

// Event image, or a coloured block with the organizer name if there isn't one.
// The title is always next to the image, so the alt text just says what the image is.
function eventImage(event, index) {
  if (!event.image_url) {
    return `<div class="no-image ${colorClass(
      index
    )}" aria-hidden="true">${escapeHtml(event.source_name)}</div>`;
  }
  return `<img src="${escapeHtml(
    event.image_url
  )}" alt="Poster for ${escapeHtml(
    event.title
  )}" loading="lazy" data-fallback="${escapeHtml(
    event.source_name
  )}" data-color="${colorClass(index)}">`;
}

// Short location for cards only. The full original address stays in Details.
function cardLocation(event) {
  const venue = String(event.venue || event.address?.venue || "").trim();
  const lines = (event.address?.lines || []).filter(Boolean);
  const title = String(event.title || "");
  const orgs = (event.organizations || []).join(" ");
  const context = [venue, ...lines, title, orgs].join(" ");
  if (
    /all\s+campuses|all\s+three\s+campuses/i.test(context) ||
    (/davis campus/i.test(venue) && /trafalgar campus/i.test(venue))
  ) {
    return venue.replace(/\s*\(map\)\s*$/i, "") || "All campuses";
  }
  if (/\bonline\b|\bvirtual\b/i.test(venue)) return "Online";

  const atHmc = /\bHMC\b|hazel mccallion|duke of york/i.test(context);
  const atDavis = /\bDAV\b|\bdavis\b|mclaughlin road/i.test(context);
  const atTraf = /\bTRAF\b|\bTRC\b|trafalgar campus|1430 trafalgar/i.test(
    context
  );
  const campus =
    [atHmc, atDavis, atTraf].filter(Boolean).length === 1
      ? atHmc
        ? "HMC"
        : atDavis
        ? "Davis Campus"
        : "Trafalgar Campus"
      : null;

  // Strip street/postal details, but keep an actual room or venue when possible.
  const cleaned = venue
    .replace(/\s*\(map\)\s*$/i, "")
    .replace(
      /\b\d{3,5}\s+(?:Duke of York Boulevard|McLaughlin Road(?: South)?|Trafalgar Road|Sheridan College Dr(?:ive)?)[^,]*(?:,?\s*(?:Oakville|Brampton|Mississauga))?/gi,
      ""
    )
    .replace(/\b(?:Oakville|Brampton|Mississauga),?\s*ON\b.*$/i, "")
    .replace(/\b[A-Z]\d[A-Z]\s*\d[A-Z]\d\b.*$/i, "")
    .replace(/\bCanada\b.*$/i, "")
    .replace(/\b(?:Davis|Trafalgar|Hazel McCallion) Campus\b/gi, "")
    .replace(/\bHMC\b|\bTRAF\b|\bTRC\b|\bDAV\b/gi, "")
    .replace(/^[\s,·-]+|[\s,·-]+$/g, "")
    .trim();
  if (campus) return cleaned ? `${campus} · ${cleaned}` : campus;

  // Off-campus: keep venue, plus city if it can be recognized.
  const cityMatch = [venue, ...lines]
    .join(" ")
    .match(/\b(Oakville|Brampton|Mississauga|Toronto)\b/i);
  const city = cityMatch?.[1];
  const shortVenue = venue
    .split(/,\s*\d{2,}|\s+\d{3,5}\s+(?=[A-Z])/)[0]
    .replace(/\s*\(map\)\s*$/i, "")
    .trim();
  if (!shortVenue) return "Location not listed";
  return city && !new RegExp(`\\b${city}\\b`, "i").test(shortVenue)
    ? `${shortVenue} · ${city}`
    : shortVenue;
}

function whereText(event) {
  const lines = event.address?.lines || [];
  const detail = lines.find((line) => line && line !== event.venue);
  return [event.venue || "Location not listed", detail]
    .filter(Boolean)
    .join(", ");
}

function priceText(event) {
  if (event.price) return event.price;
  if ((event.categories || []).some((c) => c.toLowerCase() === "free"))
    return "Free";
  return null;
}

// Original tags remain available in the detailed view.
function labels(event) {
  const all = [...(event.categories || []), ...(event.tags || [])]
    .map((label) => String(label).trim())
    .filter((label) => label && label.toLowerCase() !== "free");
  return [...new Set(all)];
}

const CARD_CATEGORIES = new Map([
  ["social", "Social"],
  ["cultural", "Cultural"],
  ["arts", "Arts"],
  ["sports", "Sports"],
  ["workshop", "Workshop"],
  ["career", "Career"],
  ["networking", "Networking"],
  ["wellness", "Wellness"],
  ["free food", "Free Food"],
  ["free stuff", "Free Stuff"],
  ["academic/educational", "Academic"],
  ["thoughtful learning", "Workshop"],
]);

// Only categories that are explicit and helpful are shown on cards.
// Club and organization names remain in Details, not as tags on cards.
function cardLabels(event) {
  const tags = [];
  for (const raw of [...(event.categories || []), ...(event.tags || [])]) {
    const label = CARD_CATEGORIES.get(String(raw).trim().toLowerCase());
    if (label && !tags.includes(label)) tags.push(label);
  }
  const price = String(event.price || "").trim();
  if (price && !/^(free|\$?0(?:\.00)?|no charge)$/i.test(price))
    tags.push("Paid");

  // Only use affirmative wording from the event's own description.
  const description = [event.excerpt, event.description]
    .filter(Boolean)
    .join(" ");
  if (
    /\b(?:registration (?:is )?required|must register|register (?:in advance|beforehand)|pre-registration required|tickets? (?:are )?required)\b/i.test(
      description
    )
  ) {
    tags.push("Registration Required");
  } else if (
    /\b(?:drop[ -]in event|free drop[ -]in|walk[ -]in registration|no registration required|no tickets? (?:are )?required)\b/i.test(
      description
    )
  ) {
    tags.push("Drop-in");
  }
  return [...new Set(tags)];
}

// Build filter options from the same labels shown on event cards.
// The original scraped tags remain unchanged for the Details popup.
function populateTagFilter(events) {
  const select = $("tag-filter");
  const previous = select.value;
  const categories = new Set();
  for (const event of events) {
    for (const label of cardLabels(event)) categories.add(label);
  }
  const preferredOrder = [
    "Social",
    "Cultural",
    "Arts",
    "Sports",
    "Workshop",
    "Career",
    "Networking",
    "Wellness",
    "Academic",
    "Free Food",
    "Free Stuff",
    "Registration Required",
    "Drop-in",
    "Paid",
  ];
  const ordered = [...categories].sort((a, b) => {
    const ia = preferredOrder.indexOf(a);
    const ib = preferredOrder.indexOf(b);
    return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib) || a.localeCompare(b);
  });
  select.replaceChildren(
    new Option("All tags", ""),
    ...ordered.map((label) => new Option(label, label))
  );
  if (categories.has(previous)) select.value = previous;
}

// Small tilted poster (hero, footer band)
function poster(event, index, { small = false, tilt = 0 } = {}) {
  return `
        <a class="poster ${small ? "small" : ""} ${colorClass(
    index
  )}" href="${escapeHtml(
    event.url
  )}" target="_blank" rel="noopener" style="transform: rotate(${tilt}deg)">
            ${eventImage(event, index)}
            <span class="text">
                <span class="source">${escapeHtml(event.source_name)}</span>
                <span class="title">${escapeHtml(event.title)}</span>
                <span class="when">${escapeHtml(mediumDate(event.start_date))}${
    event.time ? ` · ${escapeHtml(event.time)}` : ""
  }</span>
            </span>
            ${NEW_TAB}
        </a>`;
}

// One card in the "this week" carousel. Opens the details dialog.
// Clones (used to loop the auto-scroll seamlessly) are hidden from screen readers and keyboard.
function weekCard(event, index, clone = false) {
  const where = cardLocation(event);
  return `
        <li${clone ? ' aria-hidden="true"' : ""}>
            <button class="week-card" data-details="${escapeHtml(event.id)}"${
    clone ? ' tabindex="-1"' : ""
  } aria-label="${escapeHtml(event.title)}, details">
                <span class="card-media">
                    ${eventImage(event, index)}
                    <span class="date-badge" aria-hidden="true">
                        <span class="month">${escapeHtml(
                          format(event.start_date, { month: "short" })
                        )}</span>
                        <span class="day">${escapeHtml(
                          String(toDate(event.start_date).getDate())
                        )}</span>
                    </span>
                </span>
                <span class="text">
                    <span class="source">${escapeHtml(event.source_name)}</span>
                    <span class="title">${escapeHtml(event.title)}</span>
                    <span class="when">${
                      isHappeningNow(event)
                        ? '<span class="now">On now</span> '
                        : `${escapeHtml(mediumDate(event.start_date))} · `
                    }${escapeHtml(event.time || "Time not listed")}</span>
                    <span class="when">${escapeHtml(where)}</span>
                </span>
            </button>
        </li>`;
}

// ---------- top sections (built once) ----------

// Alternate sides (1st left, 2nd right, 3rd left, 4th right) so the hero stays
// balanced with 2 or 3 events. On each side the soonest event is the big one;
// the right side is mirrored in CSS (row-reverse) so its big poster sits outside.
function renderHero(upcoming) {
  const events = upcoming.slice(0, 4);
  const side = (start, tilts) =>
    events
      .map((e, i) => [e, i])
      .filter(([, i]) => i % 2 === start)
      .map(([e, i], n) => poster(e, i, { small: n === 1, tilt: tilts[n] }))
      .join("");
  $("hero-left").innerHTML = side(0, [-4, 3]);
  $("hero-right").innerHTML = side(1, [4, -3]);
}

// Swap the loading text for a wake-up note if the API is slow (Render's free
// plan sleeps when idle). Returns a function that cancels it.
function slowNotice(id, text = "Waking up the server, this can take up to a minute…") {
  const timer = setTimeout(() => ($(id).textContent = text), 5000);
  return () => clearTimeout(timer);
}

function renderLastUpdated(iso) {
  const date = iso ? new Date(iso) : null;
  if (!date || isNaN(date)) return;
  $("last-updated").textContent = `Last updated: ${date.toLocaleString("en-CA", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  })}`;
}

function renderWeek(weekEvents) {
  const today = localDateString(new Date());
  // Events that haven't started yet first; ones already running go last
  const sorted = [
    ...weekEvents.filter((e) => e.start_date >= today),
    ...weekEvents.filter((e) => e.start_date < today),
  ];
  $("topbar-count").textContent = `${weekEvents.length} event${
    weekEvents.length === 1 ? "" : "s"
  } happening this week across every campus`;

  const track = $("week-track");
  track.innerHTML =
    sorted.map((e, i) => weekCard(e, i)).join("") ||
    '<li class="week-empty">Nothing on this week yet.</li>';
  track.scrollLeft = 0;

  // Only loop (and show arrows) when the cards don't already fit on screen
  const overflowing =
    sorted.length && track.scrollWidth > track.clientWidth + 1;
  $("week-prev").hidden = $("week-next").hidden = !overflowing;
  track.classList.toggle("draggable", Boolean(overflowing));
  if (overflowing) {
    track.insertAdjacentHTML(
      "beforeend",
      sorted.map((e, i) => weekCard(e, i, true)).join("")
    );
    startCarousel(track, sorted.length);
  }
}

// ---------- carousel ----------

// The cards are rendered twice, so the track can wrap around by exactly one copy
// and look endless in both directions. It drifts on its own (paused while hovered,
// focused, touched or a details popup is open; off for reduced motion), can be
// dragged with the mouse (with a little momentum), swiped on touch screens, and
// moved one card at a time with the arrow buttons.
let carouselFrame = null;
function startCarousel(track, count) {
  cancelAnimationFrame(carouselFrame);
  const carousel = track.parentElement;
  const SPEED = 0.5; // auto-drift, px per frame (~30px/s)
  const drift = !matchMedia("(prefers-reduced-motion: reduce)").matches;

  const loopWidth = () =>
    track.children[count].offsetLeft - track.children[0].offsetLeft;
  const cardStep = () =>
    track.children[1].offsetLeft - track.children[0].offsetLeft;
  const wrap = (x) => {
    const w = loopWidth();
    return ((x % w) + w) % w;
  };

  let position = track.scrollLeft; // float, so slow drift isn't lost to rounding
  let hovered = false;
  let focused = false;
  let holdUntil = 0; // pause drift until this time after a touch, drag or arrow
  let motion = null; // { velocity } for momentum, or { from, to, start } for arrows
  const hold = (ms) => (holdUntil = performance.now() + ms);
  const setPos = (x) => {
    position = wrap(x);
    track.scrollLeft = position;
  };

  carousel.onmouseenter = () => (hovered = true);
  carousel.onmouseleave = () => (hovered = false);
  track.onfocusin = () => (focused = true);
  track.onfocusout = () => (focused = false);
  track.ontouchstart = () => {
    motion = null;
    hold(2500);
  };
  track.ontouchend = () => hold(2500);
  track.onwheel = () => hold(2500);
  // Native scrolling (touch swipe, trackpad, keyboard) moves scrollLeft directly
  track.onscroll = () => {
    if (Math.abs(track.scrollLeft - position) > 1) position = track.scrollLeft;
  };

  // Mouse drag. Touch and pen already scroll natively.
  let drag = null;
  let suppressClick = false;
  if (!track.dataset.clickGuard) {
    track.dataset.clickGuard = "1";
    track.addEventListener(
      "click",
      (e) => {
        if (track.suppressClick?.()) e.stopPropagation();
      },
      true
    );
  }
  track.suppressClick = () => suppressClick;
  track.onpointerdown = (e) => {
    if (e.pointerType !== "mouse" || e.button !== 0) return;
    motion = null;
    drag = {
      x: e.clientX,
      from: position,
      moved: false,
      lastX: e.clientX,
      lastT: e.timeStamp,
      velocity: 0,
    };
  };
  track.onpointermove = (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    if (!drag.moved && Math.abs(dx) > 5) {
      drag.moved = true;
      track.setPointerCapture(e.pointerId);
      track.classList.add("dragging");
    }
    if (!drag.moved) return;
    const dt = Math.max(1, e.timeStamp - drag.lastT);
    drag.velocity = (-(e.clientX - drag.lastX) / dt) * 16; // px per frame
    drag.lastX = e.clientX;
    drag.lastT = e.timeStamp;
    setPos(drag.from - dx);
  };
  const endDrag = (e) => {
    if (!drag) return;
    if (drag.moved) {
      track.classList.remove("dragging");
      // Swallow the click that follows a drag so it doesn't open a card
      suppressClick = true;
      setTimeout(() => (suppressClick = false), 0);
      // Only keep momentum if the mouse was still moving when released
      if (e.timeStamp - drag.lastT < 80) motion = { velocity: drag.velocity };
      hold(1500);
    }
    drag = null;
  };
  track.onpointerup = endDrag;
  track.onpointercancel = endDrag;
  track.ondragstart = (e) => e.preventDefault();

  // Arrow buttons: glide one card over
  const glide = (direction) => {
    const from = position;
    motion = {
      from,
      to: from + direction * cardStep(),
      start: performance.now(),
    };
    hold(3000);
  };
  $("week-prev").onclick = () => glide(-1);
  $("week-next").onclick = () => glide(1);

  const step = (now) => {
    if (motion?.velocity !== undefined) {
      // Momentum after a fast drag, slowing down each frame
      setPos(position + motion.velocity);
      motion.velocity *= 0.94;
      if (Math.abs(motion.velocity) < 0.3) motion = null;
      hold(1500);
    } else if (motion) {
      const t = Math.min(1, (now - motion.start) / 400);
      const ease = 1 - Math.pow(1 - t, 3);
      // Glide from the starting point; wrap() keeps it inside the loop
      track.scrollLeft = position = wrap(
        motion.from + (motion.to - motion.from) * ease
      );
      if (t === 1) motion = null;
    } else if (
      drift &&
      !drag &&
      !hovered &&
      !focused &&
      now > holdUntil &&
      !$("event-dialog").open
    ) {
      setPos(position + SPEED);
    } else if (!drag && track.scrollLeft >= loopWidth()) {
      // Scrolled by hand into the copies: wrap back so it never runs out
      setPos(track.scrollLeft);
    }
    carouselFrame = requestAnimationFrame(step);
  };
  carouselFrame = requestAnimationFrame(step);
}

// Organizer dropdown + footer links
function renderOrganizerFilters(sources) {
  const previous = $("organizer").value;
  $("footer-organizers").innerHTML = sources
    .map(
      (source) =>
        `<li><button data-organizer="${escapeHtml(source.key)}">${escapeHtml(
          source.name
        )}</button></li>`
    )
    .join("");

  $("organizer").innerHTML =
    '<option value="">All organizers</option>' +
    sources
      .map(
        (source) =>
          `<option value="${escapeHtml(source.key)}">${escapeHtml(
            source.name
          )} (${source.count})</option>`
      )
      .join("");
  $("organizer").value = previous;
}

// ---------- the events grid ----------

function eventCard(event, index) {
  const price = priceText(event);
  const tags = cardLabels(event);
  return `
        <article class="card" aria-labelledby="title-${escapeHtml(event.id)}">
            <div class="card-media">
                ${eventImage(event, index)}
                <p class="date-badge" aria-hidden="true">
                    <span class="month">${escapeHtml(
                      format(event.start_date, { month: "short" })
                    )}</span>
                    <span class="day">${escapeHtml(
                      String(toDate(event.start_date).getDate())
                    )}</span>
                </p>
            </div>
            <div class="card-body">
                <p class="source">${escapeHtml(event.source_name)}</p>
                <h3 id="title-${escapeHtml(event.id)}">${escapeHtml(
    event.title
  )}</h3>
                <dl class="facts">
                    <div><dt>Date</dt><dd>${escapeHtml(dateRange(event))}${
    isHappeningNow(event) ? ' <span class="now">On now</span>' : ""
  }</dd></div>
                    <div><dt>Time</dt><dd>${escapeHtml(
                      timeRange(event)
                    )}</dd></div>
                    <div><dt>Where</dt><dd>${escapeHtml(
                      cardLocation(event)
                    )}</dd></div>
                    ${
                      price &&
                      !/^(free|\$?0(?:\.00)?|no charge)$/i.test(
                        String(price).trim()
                      )
                        ? `<div><dt>Price</dt><dd>${escapeHtml(
                            price
                          )}</dd></div>`
                        : ""
                    }
                </dl>
                ${
                  event.excerpt
                    ? `<p class="excerpt">${escapeHtml(event.excerpt)}</p>`
                    : ""
                }
                ${
                  tags.length
                    ? `<ul class="chips" aria-label="Tags">${tags
                        .map((t) => `<li>${escapeHtml(t)}</li>`)
                        .join("")}</ul>`
                    : ""
                }
                <div class="card-actions">
                    <button class="btn btn-dark on-dark" data-details="${escapeHtml(
                      event.id
                    )}">Details<span class="visually-hidden"> for ${escapeHtml(
    event.title
  )}</span></button>
                    <a class="view-link" href="${escapeHtml(
                      event.url
                    )}" target="_blank" rel="noopener">View on ${escapeHtml(
    event.source_name
  )}<span class="visually-hidden">: ${escapeHtml(
    event.title
  )}</span>${NEW_TAB}</a>
                </div>
            </div>
        </article>`;
}

async function loadEvents() {
  $("count").textContent = "Loading events…";
  $("events-grid").setAttribute("aria-busy", "true");
  const cancelNotice = slowNotice("count");
  try {
    showResults(await fetchJson(buildEventsUrl()));
  } catch (error) {
    $("count").textContent = "";
    $(
      "events-grid"
    ).innerHTML = `<p class="empty">Could not load events (${escapeHtml(
      error.message
    )}). Is the backend running? Start it with "node server.js" in the backend folder.</p>`;
  } finally {
    cancelNotice();
    $("events-grid").removeAttribute("aria-busy");
  }
}

function showResults(data) {
    const selectedTag = $("tag-filter").value;
    const events = selectedTag
      ? data.events.filter((event) => cardLabels(event).includes(selectedTag))
      : data.events;
    state.results = events;
    for (const event of events) state.eventsById.set(event.id, event);

    const filtersUsed = [VIEW_NAMES[state.view]];
    for (const id of ["organizer", "campus", "time", "tag-filter"]) {
      if ($(id).value)
        filtersUsed.push(
          $(id).selectedOptions[0].textContent.replace(/\s*\(.*\)$/, "")
        );
    }
    $("count").textContent = `Showing ${events.length} event${
      events.length === 1 ? "" : "s"
    } · ${filtersUsed.join(", ")}`;

    $("events-grid").innerHTML = events.length
      ? events.map(eventCard).join("")
      : '<p class="empty">No events match these filters. Try another campus, organizer, time or tag.</p>';
}

function setView(view) {
  state.view = view;
  for (const button of $("views").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  }
  loadEvents();
}

function goToEvents() {
  $("events").scrollIntoView();
  $("events-title").setAttribute("tabindex", "-1");
  $("events-title").focus({ preventScroll: true });
}

// ---------- event details dialog ----------

// Scraped descriptions (SSU especially) include the rest of the web page: the
// date/address header, "Google Calendar / ICS" link text, a "More events" list,
// the newsletter form and house-rules boilerplate. Trim that off for display only;
// the scraped data itself is left untouched.
const DESCRIPTION_STOP =
  /^(more events|newsletter block|posted in|house rules|notice of (?:photography|filming)|want the inside scoop|featured$)/i;
const DESCRIPTION_JUNK =
  /^(\(map\)|google calendar|ics|\||\.|,|register now|read more\s*→?|share|\d+ likes?|tagged|canada)$/i;

function cleanDescription(event) {
  let lines = String(event.description || "")
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);
  // SSU pages repeat the title/date/time/address before the real text; that header ends at "ICS"
  const icsAt = lines.slice(0, 25).findIndex((l) => /^ics$/i.test(l));
  if (icsAt >= 0) lines = lines.slice(icsAt + 1);

  const kept = [];
  for (let line of lines) {
    if (DESCRIPTION_STOP.test(line)) break;
    // Sheridan pages start with "Date: … Time: … Location: Online", already shown above
    line = line
      .replace(/^date\s*:.*?time\s*:.*?location\s*:\s*\S+\s*/i, "")
      .trim();
    if (!line || DESCRIPTION_JUNK.test(line) || line === event.title) continue;
    kept.push(line);
  }
  return kept;
}

// Short "good to know" badges pulled from the event's own wording
function goodToKnow(event) {
  // Use the cleaned text so badges from the "More events" list on SSU pages don't leak in
  const text = [event.title, event.sales_status, ...cleanDescription(event)]
    .filter(Boolean)
    .join(" ");
  const notes = [];
  if (/\bsold out\b/i.test(text)) notes.push("Sold out");
  if (/students only|open to sheridan students/i.test(text))
    notes.push("Sheridan students only");
  if (/\b18\+/.test(text)) notes.push("18+");
  if (/\bonecard\b/i.test(text)) notes.push("Bring your OneCard");
  for (const label of cardLabels(event)) {
    if (label === "Registration Required" || label === "Drop-in")
      notes.push(label);
  }
  return [...new Set(notes)];
}

function openDetails(id, trigger) {
  const event = state.eventsById.get(id);
  if (!event) return;
  state.lastFocus = trigger;

  const price = priceText(event);
  const tags = labels(event);
  const notes = goodToKnow(event);
  const occurrences = event.occurrences || [];
  const addressLines = (event.address?.lines || []).filter(Boolean);
  const address = addressLines.join(", ");
  const showAddress = address && address !== event.venue;

  const body = cleanDescription(event);
  const summary = event.excerpt || body[0] || "";
  // Only offer the full text when it says meaningfully more than the summary
  const hasMore = body.join(" ").length > summary.length + 60;

  $("dialog-content").innerHTML = `
        ${
          event.image_url
            ? `<img class="dialog-image" src="${escapeHtml(
                event.image_url
              )}" alt="Poster for ${escapeHtml(event.title)}">`
            : ""
        }
        <div class="dialog-body">
            <div>
                <p class="eyebrow">${escapeHtml(event.source_name)}${
    event.organizations?.length
      ? ` · ${escapeHtml(event.organizations.join(", "))}`
      : ""
  }</p>
                <h2 id="dialog-title">${escapeHtml(event.title)}</h2>
            </div>
            ${summary ? `<p class="summary">${escapeHtml(summary)}</p>` : ""}
            <dl class="facts">
                <div><dt>Date</dt><dd>${escapeHtml(dateRange(event))}${
    isHappeningNow(event) ? ' <span class="now">On now</span>' : ""
  }</dd></div>
                <div><dt>Time</dt><dd>${escapeHtml(timeRange(event))}</dd></div>
                <div><dt>Where</dt><dd>${escapeHtml(
                  String(event.venue || "").replace(/\s*\(map\)\s*$/i, "") ||
                    "Location not listed"
                )}${
    !showAddress && event.address?.map_url
      ? ` <a href="${escapeHtml(
          event.address.map_url
        )}" target="_blank" rel="noopener">Map${NEW_TAB}</a>`
      : ""
  }</dd></div>
                ${
                  showAddress
                    ? `<div><dt>Address</dt><dd>${escapeHtml(address)}${
                        event.address?.map_url
                          ? ` <a href="${escapeHtml(
                              event.address.map_url
                            )}" target="_blank" rel="noopener">Map${NEW_TAB}</a>`
                          : ""
                      }</dd></div>`
                    : ""
                }
                ${
                  price
                    ? `<div><dt>Price</dt><dd>${escapeHtml(price)}</dd></div>`
                    : ""
                }
                ${
                  event.sales_status
                    ? `<div><dt>Tickets</dt><dd>${escapeHtml(
                        event.sales_status
                      )}</dd></div>`
                    : ""
                }
            </dl>
            ${
              notes.length
                ? `<section><h3>Good to know</h3><ul class="chips good-to-know">${notes
                    .map((n) => `<li>${escapeHtml(n)}</li>`)
                    .join("")}</ul></section>`
                : ""
            }
            ${
              occurrences.length > 1
                ? `<section><h3>All dates</h3><ul>${occurrences
                    .map(
                      (o) =>
                        `<li>${escapeHtml(longDate(o.date))}${
                          o.time_12h ? ` at ${escapeHtml(o.time_12h)}` : ""
                        }</li>`
                    )
                    .join("")}</ul></section>`
                : ""
            }
            ${
              hasMore
                ? `<details class="full-desc">
                    <summary>Read the full description</summary>
                    <div class="description">${body
                      .map((line) => `<p>${escapeHtml(line)}</p>`)
                      .join("")}</div>
                   </details>`
                : ""
            }
            ${
              tags.length
                ? `<ul class="chips" aria-label="Tags">${tags
                    .map((t) => `<li>${escapeHtml(t)}</li>`)
                    .join("")}</ul>`
                : ""
            }
            <div class="dialog-links">
                <a class="btn btn-dark on-dark" href="${escapeHtml(
                  event.url
                )}" target="_blank" rel="noopener">View on ${escapeHtml(
    event.source_name
  )}${NEW_TAB}</a>
                ${
                  event.google_calendar_url
                    ? `<a class="btn btn-light" href="${escapeHtml(
                        event.google_calendar_url
                      )}" target="_blank" rel="noopener">Add to Google Calendar${NEW_TAB}</a>`
                    : ""
                }
                ${
                  event.ics_url
                    ? `<a class="btn btn-light" href="${escapeHtml(
                        event.ics_url
                      )}">Download calendar file</a>`
                    : ""
                }
            </div>
        </div>`;

  $("event-dialog").showModal();
  $("dialog-close").focus();
}

function closeDetails() {
  $("event-dialog").close();
}

$("event-dialog").addEventListener("close", () => {
  state.lastFocus?.focus();
});
$("dialog-close").addEventListener("click", closeDetails);
// Clicking the dark backdrop closes it too
$("event-dialog").addEventListener("click", (e) => {
  if (e.target === $("event-dialog")) closeDetails();
});

// ---------- wiring up ----------

// Swap a broken image for the coloured block with the organizer name
document.addEventListener(
  "error",
  (e) => {
    const img = e.target;
    if (!(img instanceof HTMLImageElement) || !img.dataset.fallback) return;
    const block = document.createElement("div");
    block.className = `no-image ${img.dataset.color}`;
    block.setAttribute("aria-hidden", "true");
    block.textContent = img.dataset.fallback;
    img.replaceWith(block);
  },
  true
);

$("views").addEventListener("click", (e) => {
  const button = e.target.closest("button[data-view]");
  if (button) setView(button.dataset.view);
});

for (const id of ["organizer", "campus", "time", "tag-filter"]) {
  $(id).addEventListener("change", loadEvents);
}

document.addEventListener("click", (e) => {
  const details = e.target.closest("[data-details]");
  const viewButton = e.target.closest("[data-show-view]");
  const organizerButton = e.target.closest("[data-organizer]");
  const campusButton = e.target.closest("[data-campus]");
  const scrollButton = e.target.closest("[data-scroll]");

  if (details) {
    openDetails(details.dataset.details, details);
  } else if (viewButton) {
    setView(viewButton.dataset.showView);
    goToEvents();
  } else if (organizerButton) {
    $("organizer").value = organizerButton.dataset.organizer;
    loadEvents();
    goToEvents();
  } else if (campusButton) {
    $("campus").value = campusButton.dataset.campus;
    loadEvents();
    goToEvents();
  } else if (scrollButton) {
    goToEvents();
  }
});

// ---------- start ----------

// ---------- saved copy (instant first paint) ----------

// The daily collector also publishes events.json with this site. Drawing the
// page from it means visitors see events right away, even while the API
// (Render's free plan) is still waking up. The API is checked afterwards.
async function fetchSnapshot() {
  try {
    const response = await fetch("events.json", { cache: "no-cache" });
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

function addDays(dateString, days) {
  const date = new Date(`${dateString}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

// Same rule as the API (backend/api/server.js): a one-date event with a later
// end (e.g. a 6-week program) runs every day in between.
function happensBetween(event, from, to) {
  const dates = (event.occurrences || []).map((o) => o.date);
  if (dates.length === 1 && event.end_date > event.start_date) {
    return event.start_date <= to && event.end_date >= from;
  }
  return dates.some((date) => date >= from && date <= to);
}

// The saved file -> the same { sources, all, week } the API endpoints return
function snapshotViews(data) {
  const today = localDateString(new Date());
  const events = data.events.filter(
    (e) => (e.end_date || e.start_date) >= today
  );
  const weekday = new Date(`${today}T00:00:00Z`).getUTCDay(); // 0 = Sunday
  const monday = addDays(today, weekday === 0 ? -6 : 1 - weekday);
  const sunday = addDays(monday, 6);
  return {
    sources: Object.entries(data.sources || {}).map(([key, source]) => ({
      key,
      name: source.name,
      count: events.filter((e) => e.source === key).length,
    })),
    all: { events, last_updated: data.last_updated },
    week: { events: events.filter((e) => happensBetween(e, monday, sunday)) },
  };
}

function renderTop({ sources, all, week }) {
  renderLastUpdated(all.last_updated || sources[0]?.last_updated);
  for (const event of all.events) state.eventsById.set(event.id, event);
  populateTagFilter(all.events);

  const today = localDateString(new Date());
  // Include events that are still running, soonest first
  const upcoming = all.events
    .filter((e) => (e.end_date || e.start_date) >= today)
    .sort((a, b) => a.start_date.localeCompare(b.start_date));

  renderHero(upcoming);
  for (const event of week.events) state.eventsById.set(event.id, event);
  renderWeek(week.events);
  renderOrganizerFilters(sources);
}

async function init() {
  $("year").textContent = new Date().getFullYear();

  const snapshot = await fetchSnapshot();
  // Browsers can restore dropdowns on reload; only the unfiltered grid can
  // come straight from the saved copy.
  const filtered = ["organizer", "campus", "time", "tag-filter"].some(
    (id) => $(id).value
  );
  if (snapshot) {
    const views = snapshotViews(snapshot);
    renderTop(views);
    if (filtered) loadEvents();
    else showResults(views.all);
  } else {
    loadEvents();
  }

  const cancelNotice = snapshot ? () => {} : slowNotice("topbar-count");
  try {
    const [sources, all, week] = await Promise.all([
      fetchJson("/api/sources"),
      fetchJson("/api/events"),
      fetchJson("/api/events/week"),
    ]).finally(cancelNotice);
    // Usually the API has the same data as the saved copy: nothing to redraw
    if (snapshot && all.last_updated === snapshot.last_updated) return;
    renderTop({ sources, all, week });
    // Redraw the grid with the newer data, keeping the visitor's filters.
    // (Without a saved copy, this also reapplies a restored tag filter.)
    if (snapshot || $("tag-filter").value) await loadEvents();
  } catch (error) {
    if (!snapshot) $("topbar-count").textContent = "Could not reach the events API";
    console.error(error);
  }
}

init();
