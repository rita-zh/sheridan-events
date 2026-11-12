import express from 'express';
import { readFileSync, statSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

// data/events.json at the repo root, written daily by the GitHub Actions collector
// (see collector/ and .github/workflows/collect-events.yml). Override with DATA_FILE.
const DATA_FILE = process.env.DATA_FILE
    || join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'data', 'events.json');

const app = express();
const PORT = process.env.PORT || 3000;
const TIME_ZONE = 'America/Toronto';

// The short names used in the URL, e.g. /api/events/source/ssu
const SOURCES = [
    { key: 'ssu', name: 'SSU' },
    { key: 'clubs', name: 'Sheridan Clubs' },
    { key: 'edge', name: 'EDGE' },
    { key: 'sheridan', name: 'Sheridan College' },
];

// Short names people use for campuses -> words to look for in the event
const LOCATION_ALIASES = {
    hmc: ['hmc', 'hazel mccallion', 'mississauga'],
    mississauga: ['hmc', 'hazel mccallion', 'mississauga'],
    davis: ['davis', 'dav', 'brampton'],
    brampton: ['davis', 'dav', 'brampton'],
    trafalgar: ['trafalgar', 'traf', 'trc', 'oakville'],
    oakville: ['trafalgar', 'traf', 'trc', 'oakville'],
    online: ['online'],
};

const TIMES_OF_DAY = {
    morning: ['00:00', '11:59'],
    afternoon: ['12:00', '16:59'],
    evening: ['17:00', '23:59'],
};


// ---------- dates (all "YYYY-MM-DD" strings in Toronto time) ----------

function localDate(date) {
    return new Intl.DateTimeFormat('en-CA', { timeZone: TIME_ZONE }).format(date);
}

function today() {
    return localDate(new Date());
}

function addDays(dateString, days) {
    const date = new Date(`${dateString}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + days);
    return date.toISOString().slice(0, 10);
}

// Monday to Sunday of the week that contains dateString
function weekRange(dateString) {
    const weekday = new Date(`${dateString}T00:00:00Z`).getUTCDay(); // 0 = Sunday
    const monday = addDays(dateString, weekday === 0 ? -6 : 1 - weekday);
    return [monday, addDays(monday, 6)];
}

const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];

// "2026-10", "10", "oct" or "october" -> { year, month: "10" }
function parseMonth(value) {
    const text = String(value).toLowerCase();
    const yearMonth = text.match(/^(\d{4})-(\d{1,2})$/);
    if (yearMonth) return { year: yearMonth[1], month: yearMonth[2].padStart(2, '0') };
    if (/^\d{1,2}$/.test(text)) return { year: null, month: text.padStart(2, '0') };
    const index = MONTHS.indexOf(text.slice(0, 3));
    return index === -1 ? null : { year: null, month: String(index + 1).padStart(2, '0') };
}


// ---------- loading the collected data ----------

// The file is already in the final event shape. Keep it in memory and only
// re-read it when it changes, so responses are instant (even right after a
// cold start). If a read ever fails, keep serving the last good copy.
const EMPTY = { last_updated: null, sources: {}, events: [] };
let cache = { mtimeMs: null, data: EMPTY };

function loadData() {
    try {
        const { mtimeMs } = statSync(DATA_FILE);
        if (mtimeMs !== cache.mtimeMs) {
            const data = JSON.parse(readFileSync(DATA_FILE, 'utf-8'));
            if (!Array.isArray(data.events)) throw new Error('no events array');
            cache = { mtimeMs, data };
        }
    } catch (error) {
        if (cache.mtimeMs === null) console.error(`Could not load ${DATA_FILE}: ${error.message}`);
    }
    return cache.data;
}

// Upcoming and ongoing events only: the file is refreshed once a day, so
// drop anything that has finished since it was written.
function loadEvents() {
    const now = today();
    return loadData().events.filter(e => (e.end_date || e.start_date) >= now);
}

// True if the event happens on any day between from and to (inclusive).
// Events with one listed date but a later end (e.g. a 6-week program) count
// as running every day in between; others only count on their listed dates.
function happensBetween(event, from, to) {
    const dates = (event.occurrences || []).map(o => o.date);
    const isOngoing = dates.length === 1 && event.end_date > event.start_date;
    if (isOngoing) {
        return event.start_date <= to && event.end_date >= from;
    }
    return dates.some(date => date >= from && date <= to);
}


// ---------- one filter used by every endpoint ----------

function filterEvents(events, filters) {
    let results = events;
    const { source, date, from, to, month, week, location, time, category, search } = filters;

    if (source) {
        results = results.filter(e => e.source === source.toLowerCase());
    }

    if (date) {
        results = results.filter(e => happensBetween(e, date, date));
    }

    if (from || to) {
        results = results.filter(e => happensBetween(e, from || '0000-00-00', to || '9999-12-31'));
    }

    if (week) {
        const [monday, sunday] = weekRange(week);
        results = results.filter(e => happensBetween(e, monday, sunday));
    }

    if (month) {
        const { year, month: mm } = parseMonth(month) || {};
        results = results.filter(e => {
            // With no year given ("oct"), check every year the event runs in
            const startYear = Number(e.start_date.slice(0, 4));
            const endYear = Number(e.end_date.slice(0, 4));
            const years = year ? [year] : Array.from({ length: endYear - startYear + 1 }, (_, i) => startYear + i);
            return years.some(y => happensBetween(e, `${y}-${mm}-01`, `${y}-${mm}-31`));
        });
    }

    if (location) {
        const words = LOCATION_ALIASES[location.toLowerCase()] || [location.toLowerCase()];
        results = results.filter(e => {
            const text = [e.venue, e.title, ...(e.address?.lines || []), ...(e.categories || [])]
                .join(' ')
                .toLowerCase();
            return words.some(word => new RegExp(`\\b${word}\\b`).test(text));
        });
    }

    if (time) {
        const range = TIMES_OF_DAY[time.toLowerCase()];
        // "morning" / "afternoon" / "evening", or an hour like "14" or "14:00"
        const [start, end] = range || [time.padStart(2, '0').slice(0, 2) + ':00', time.padStart(2, '0').slice(0, 2) + ':59'];
        results = results.filter(e => e.time_24h && e.time_24h >= start && e.time_24h <= end);
    }

    if (category) {
        const wanted = category.toLowerCase();
        results = results.filter(e =>
            [...(e.categories || []), ...(e.tags || [])].some(c => c.toLowerCase() === wanted)
        );
    }

    if (search) {
        const term = search.toLowerCase();
        results = results.filter(e =>
            [e.title, e.excerpt, e.venue].join(' ').toLowerCase().includes(term)
        );
    }

    return results;
}

// Send filtered events; query params (?location=hmc&time=evening) stack on top of any endpoint
function sendEvents(req, res, filters = {}) {
    const allFilters = { ...req.query, ...filters };
    const events = filterEvents(loadEvents(), allFilters);
    res.json({ count: events.length, last_updated: loadData().last_updated, filters: allFilters, events });
}


// ---------- middleware ----------

// The front-end is hosted separately, so let it call this API from any origin
app.use((req, res, next) => {
    res.set('Access-Control-Allow-Origin', '*');
    next();
});


// ---------- endpoints ----------

// List of every endpoint, handy while building the front-end
app.get('/api', (req, res) => {
    res.json({
        endpoints: {
            'GET /api/events': 'All events from every source (accepts any filter below as ?query=)',
            'GET /api/events/:id': 'One event, e.g. /api/events/ssu-ping-pong-tournament',
            'GET /api/sources': 'The scraped sites and how many events each has',
            'GET /api/events/source/:source': `Events from one site: ${SOURCES.map(s => s.key).join(', ')}`,
            'GET /api/events/today': 'Events happening today',
            'GET /api/events/upcoming': 'Events in the next 7 days (?days=14 to change)',
            'GET /api/events/date/:date': 'Events on a day, e.g. /api/events/date/2026-10-14',
            'GET /api/events/week': 'Events this week (Mon-Sun)',
            'GET /api/events/week/:date': 'Events in the week containing a date',
            'GET /api/events/month/:month': 'Events in a month: 2026-10, 10, oct or october',
            'GET /api/events/time/:time': 'morning, afternoon, evening, or an hour like 14',
            'GET /api/events/location/:location': 'hmc, davis, trafalgar, online, or any venue word',
        },
        query_filters: ['source', 'date', 'from', 'to', 'week', 'month', 'time', 'location', 'category', 'search'],
        example: '/api/events/month/oct?location=hmc&time=evening',
    });
});

// Still an array (the front-end maps over it). Each entry also carries the
// file-wide last_updated plus that source's own last_success / status.
app.get('/api/sources', (req, res) => {
    const { last_updated, sources = {} } = loadData();
    const events = loadEvents();
    res.json(SOURCES.map(({ key, name }) => ({
        key,
        name,
        count: events.filter(e => e.source === key).length,
        source_url: sources[key]?.source_url || events.find(e => e.source === key)?.source_url || null,
        endpoint: `/api/events/source/${key}`,
        status: sources[key]?.status || 'unknown',
        last_success: sources[key]?.last_success || null,
        last_updated,
    })));
});

app.get('/api/events', (req, res) => sendEvents(req, res));

app.get('/api/events/source/:source', (req, res) => {
    if (!SOURCES.some(s => s.key === req.params.source)) {
        return res.status(404).json({ error: `Unknown source. Use one of: ${SOURCES.map(s => s.key).join(', ')}` });
    }
    sendEvents(req, res, { source: req.params.source });
});

app.get('/api/events/today', (req, res) => sendEvents(req, res, { date: today() }));

app.get('/api/events/upcoming', (req, res) => {
    const days = Number(req.query.days) || 7;
    sendEvents(req, res, { from: today(), to: addDays(today(), days - 1) });
});

app.get('/api/events/date/:date', (req, res) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(req.params.date)) {
        return res.status(400).json({ error: 'Use a date like 2026-10-14' });
    }
    sendEvents(req, res, { date: req.params.date });
});

app.get('/api/events/week', (req, res) => sendEvents(req, res, { week: today() }));

app.get('/api/events/week/:date', (req, res) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(req.params.date)) {
        return res.status(400).json({ error: 'Use a date like 2026-10-14' });
    }
    sendEvents(req, res, { week: req.params.date });
});

app.get('/api/events/month/:month', (req, res) => {
    if (!parseMonth(req.params.month)) {
        return res.status(400).json({ error: 'Use a month like 2026-10, 10, oct or october' });
    }
    sendEvents(req, res, { month: req.params.month });
});

app.get('/api/events/time/:time', (req, res) => {
    const { time } = req.params;
    if (!TIMES_OF_DAY[time.toLowerCase()] && !/^\d{1,2}(:\d{2})?$/.test(time)) {
        return res.status(400).json({ error: 'Use morning, afternoon, evening, or an hour like 14' });
    }
    sendEvents(req, res, { time });
});

app.get('/api/events/location/:location', (req, res) => sendEvents(req, res, { location: req.params.location }));

// Keep this after the other /api/events/... routes so it doesn't swallow them
app.get('/api/events/:id', (req, res) => {
    const event = loadEvents().find(e => e.id === req.params.id);
    if (!event) {
        return res.status(404).json({ error: 'Event not found' });
    }
    res.json(event);
});

// Unknown /api/... paths get JSON instead of an HTML error page
app.use('/api', (req, res) => {
    res.status(404).json({ error: 'Unknown endpoint. See /api for the list.' });
});

// Run locally with `node api/server.js` from backend/; Vercel imports the app instead
if (!process.env.VERCEL) {
    app.listen(PORT, () => console.log(`Listening on http://localhost:${PORT}`));
}

export default app;
