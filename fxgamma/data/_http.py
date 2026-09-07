"""Shared HTTP plumbing for the live adapters.

Design rule for this package (see docs/04_data_sources.md, "Testability"):

    fetch_*()  -- does network I/O only, returns raw bytes/str/json
    parse_*()  -- pure function, raw payload -> DataFrame / dataclasses

Nothing that parses may touch the network, so every parser is unit-testable against the
recorded fixtures in ``fxgamma/data/fixtures/`` with zero connectivity.  The build sandbox
blocks outbound access to every market-data host (proxy answers ``403`` to ``CONNECT``), so
the parsers are the only part of the live path that is actually exercised here.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

log = logging.getLogger(__name__)

__all__ = [
    "USER_AGENT", "HttpError", "OfflineError", "HttpConfig", "get", "get_json",
    "get_text", "get_bytes", "session", "network_enabled",
]

# Descriptive UA: public endpoints (Stooq, ECB, CME) block generic python-requests UAs and
# we want to be identifiable/contactable rather than anonymous.
USER_AGENT = os.environ.get(
    "FXGAMMA_USER_AGENT",
    "fxgamma/0.1 (FX gamma research dashboard; non-commercial; +https://github.com/)",
)

# Per-host minimum spacing in seconds. We never hammer a free public endpoint.
_HOST_MIN_INTERVAL = {
    "query1.finance.yahoo.com": 1.0,
    "query2.finance.yahoo.com": 1.0,
    "stooq.com": 2.0,
    "www.ecb.europa.eu": 1.0,
    "data-api.ecb.europa.eu": 1.0,
    "fred.stlouisfed.org": 1.0,
    "api.stlouisfed.org": 0.5,
    "www.cmegroup.com": 2.0,
    "cdn.cboe.com": 1.0,
}
_DEFAULT_MIN_INTERVAL = 0.5
_last_hit: dict[str, float] = {}
_lock = threading.Lock()


class HttpError(RuntimeError):
    """Any non-retryable HTTP/parse-level failure from a live source."""


class OfflineError(HttpError):
    """Network unreachable (DNS, proxy CONNECT refusal, timeout after retries)."""


@dataclass(frozen=True)
class HttpConfig:
    timeout: float = 15.0          # connect+read, seconds
    retries: int = 3               # total attempts = retries
    backoff: float = 0.8           # exponential base, jittered
    max_backoff: float = 8.0


DEFAULT = HttpConfig()

_session = None


def network_enabled() -> bool:
    """Escape hatch so CI / the sandbox can hard-disable every live call."""
    return os.environ.get("FXGAMMA_OFFLINE", "").strip().lower() not in ("1", "true", "yes")


def session():
    """Lazily-built :class:`requests.Session` with the project User-Agent."""
    global _session
    if _session is None:
        import requests  # imported lazily so the synthetic path needs no requests

        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        _session = s
    return _session


def _throttle(host: str) -> None:
    gap = _HOST_MIN_INTERVAL.get(host, _DEFAULT_MIN_INTERVAL)
    with _lock:
        prev = _last_hit.get(host, 0.0)
        wait = gap - (time.monotonic() - prev)
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.monotonic()


def get(url: str, *, params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        cfg: HttpConfig = DEFAULT):
    """GET with per-host throttling, timeout and exponential backoff.

    Retries on 429/5xx and on transport errors. Raises :class:`OfflineError` when the host
    is unreachable and :class:`HttpError` on a definitive 4xx.
    """
    if not network_enabled():
        raise OfflineError(f"FXGAMMA_OFFLINE is set; refusing to fetch {url}")

    import requests
    from urllib.parse import urlparse

    host = urlparse(url).netloc
    last: Exception | None = None
    for attempt in range(1, cfg.retries + 1):
        _throttle(host)
        try:
            r = session().get(url, params=params, headers=dict(headers or {}),
                              timeout=cfg.timeout)
        except requests.exceptions.RequestException as exc:   # DNS / proxy / TLS / timeout
            last = exc
            log.debug("%s attempt %d transport error: %s", url, attempt, exc)
        else:
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                last = HttpError(f"{r.status_code} from {host}")
                log.debug("%s attempt %d HTTP %d", url, attempt, r.status_code)
            else:
                raise HttpError(f"HTTP {r.status_code} from {url}: {r.text[:200]!r}")
        if attempt < cfg.retries:
            time.sleep(min(cfg.max_backoff, cfg.backoff * 2 ** (attempt - 1))
                       * (0.75 + 0.5 * random.random()))
    raise OfflineError(f"{url} unreachable after {cfg.retries} attempts: {last}")


def get_text(url: str, **kw) -> str:
    return get(url, **kw).text


def get_bytes(url: str, **kw) -> bytes:
    return get(url, **kw).content


def get_json(url: str, **kw):
    r = get(url, **kw)
    try:
        return r.json()
    except ValueError as exc:
        raise HttpError(f"non-JSON response from {url}: {r.text[:200]!r}") from exc
