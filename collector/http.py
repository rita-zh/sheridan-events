"""One polite HTTP client shared by every collector.

- sends a normal browser User-Agent (override with COLLECTOR_USER_AGENT)
- one request at a time, with a pause between requests
- a timeout on every request
- retries with backoff on connection errors, 429 and 5xx (honours Retry-After)
"""

import logging
import os
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# A normal desktop browser User-Agent (some sites serve bots a different page)
USER_AGENT = os.environ.get(
    "COLLECTOR_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
)
TIMEOUT = 30          # seconds, per request
DELAY = 1.0           # seconds between requests


class Http:
    def __init__(self, delay=DELAY, timeout=TIMEOUT):
        self.delay = delay
        self.timeout = timeout
        self._last = 0.0
        self.requests_made = 0

        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=2,  # 2s, 4s, 8s
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,  # let raise_for_status() give a readable error
        )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-CA,en;q=0.8"})
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def get(self, url, *, params=None, headers=None):
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        finally:
            self._last = time.monotonic()
            self.requests_made += 1
        log.debug("GET %s -> %s", response.url, response.status_code)
        response.raise_for_status()
        return response

    def json(self, url, **kwargs):
        response = self.get(url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise ValueError(f"{url} did not return JSON ({response.headers.get('content-type')})") from exc

    def text(self, url, **kwargs):
        return self.get(url, **kwargs).text
