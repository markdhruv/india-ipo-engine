"""One polite, retrying HTTP session.

NSE serves its JSON APIs to anything with a browser-ish User-Agent and a
plausible Referer, but it rate-limits and occasionally 403s a cold session, so
every request retries with backoff and we re-warm cookies on a 403.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class Fetcher:
    def __init__(self, cfg: dict):
        http = cfg.get("http", {})
        self.timeout = http.get("timeout_sec", 25)
        self.retries = http.get("retries", 3)
        self.backoff = http.get("backoff_sec", 2.0)
        self.delay = http.get("polite_delay_sec", 1.0)
        self._last_call = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        })
        self._warmed: set[str] = set()

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_call = time.monotonic()

    def warm(self, url: str) -> None:
        """Visit an HTML page first so the host hands us cookies."""
        if url in self._warmed:
            return
        self._warmed.add(url)
        try:
            self._throttle()
            self.session.get(url, timeout=self.timeout, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        except requests.RequestException as exc:
            log.debug("warm-up of %s failed (harmless): %s", url, exc)

    def get(self, url: str, *, referer: str | None = None, as_json: bool = False,
            warm: str | None = None):
        """GET with retries. Returns parsed JSON, response text, or None."""
        if warm:
            self.warm(warm)
        headers = {"Accept": "application/json, text/plain, */*" if as_json
                   else "text/html,application/xhtml+xml,*/*;q=0.8"}
        if referer:
            headers["Referer"] = referer

        last_error: str | None = None
        for attempt in range(1, self.retries + 1):
            try:
                self._throttle()
                resp = self.session.get(url, timeout=self.timeout, headers=headers)
                if resp.status_code == 403 and warm:
                    self._warmed.discard(warm)   # cookies went stale; re-warm
                    self.warm(warm)
                    last_error = "403"
                elif resp.status_code in (404, 410):
                    # Definitive: the symbol does not exist on this exchange.
                    # Retrying triples the cost of a backfill for nothing,
                    # and unlisted SME symbols miss constantly.
                    return None
                elif resp.status_code >= 400:
                    last_error = f"HTTP {resp.status_code}"
                elif as_json:
                    try:
                        return resp.json()
                    except ValueError:
                        last_error = f"non-JSON body ({len(resp.text)} bytes)"
                else:
                    return resp.text
            except requests.RequestException as exc:
                last_error = type(exc).__name__
            if attempt < self.retries:
                time.sleep(self.backoff * attempt)

        log.warning("GET failed after %d tries (%s): %s", self.retries, last_error, url)
        return None
