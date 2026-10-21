"""Polite, budgeted, SSRF-safe HTTP fetching that snapshots every successful body."""
from __future__ import annotations

import asyncio
import ipaddress
import time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .store import SnapshotStore
from .util import now_iso


class Ctx:
    """Per-company budget and error log."""

    def __init__(self, orgnr: str, deadline: float, *, registry_only: bool = False):
        self.orgnr = orgnr
        self.deadline = deadline
        self.registry_only = registry_only
        self.started = time.monotonic()
        self.requests = 0
        self.errors: list[dict] = []

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def expired(self, reserve: float = 0.0) -> bool:
        return self.remaining() <= reserve

    def error(self, stage: str, kind: str, message: str = "", url: str | None = None) -> None:
        self.errors.append({"stage": stage, "kind": kind, "message": (message or "")[:300],
                            "url": url, "at": now_iso()})


@dataclass
class FetchResult:
    url: str
    ok: bool = False
    status: int | None = None
    final_url: str | None = None
    content: bytes = b""
    content_type: str = ""
    encoding: str | None = None
    error: str | None = None  # budget|robots|bad_url|unsafe_url|timeout|network|blocked|not_found|http_error
    detail: str | None = None
    snapshot: dict | None = None
    redirects: list = field(default_factory=list)
    truncated: bool = False
    _text: str | None = None
    _soup: object = None

    @property
    def text(self) -> str:
        if self._text is None:
            for enc in (self.encoding, "utf-8", "cp1252"):
                if not enc:
                    continue
                try:
                    self._text = self.content.decode(enc)
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            if self._text is None:
                self._text = self.content.decode("utf-8", "replace")
        return self._text

    def json(self):
        import json

        return json.loads(self.text)

    @property
    def is_html(self) -> bool:
        ct = (self.content_type or "").lower()
        if "html" in ct:
            return True
        if ct and not ct.startswith("text/plain"):
            return False
        head = self.text[:200].lstrip().lower()
        return head.startswith("<!doctype html") or head.startswith("<html")

    @property
    def where(self) -> str:
        return self.final_url or self.url

    def soup(self):
        if self._soup is None:
            from bs4 import BeautifulSoup

            try:
                self._soup = BeautifulSoup(self.text, "lxml")
            except Exception:  # pragma: no cover - parser fallback
                self._soup = BeautifulSoup(self.text, "html.parser")
        return self._soup


_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home", ".corp", ".intranet")


def safe_host(host: str | None) -> bool:
    """Refuse loopback, private, link-local and other non-public targets."""
    if not host:
        return False
    host = host.strip("[]").lower().rstrip(".")
    if host in ("localhost",) or host.endswith(_BLOCKED_SUFFIXES) or "." not in host and ":" not in host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


class UnsafeRedirect(httpx.HTTPError):
    pass


class Fetcher:
    def __init__(self, store: SnapshotStore | None, *, user_agent: str, ua_token: str,
                 global_concurrency: int = 48, per_host: int = 2, timeout: float = 12.0,
                 max_bytes: int = 2_500_000, respect_robots: bool = True,
                 global_deadline: float | None = None):
        self.store = store
        self.ua_token = ua_token
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self.global_deadline = global_deadline
        self.request_count = 0
        self.bytes_in = 0
        self._global_sem = asyncio.Semaphore(global_concurrency)
        self._per_host = per_host
        self._host_limits = {"data.brreg.no": 16, "w2.brreg.no": 4, "pam-stilling-feed.nav.no": 6,
                             "rdap.norid.no": 3, "query.wikidata.org": 1}
        self._host_sems: dict[str, asyncio.Semaphore] = {}
        self._robots: dict[str, asyncio.Future] = {}
        self.host_errors: dict[str, int] = defaultdict(int)

        async def _guard(request: httpx.Request):
            if not safe_host(request.url.host):
                raise UnsafeRedirect(f"refusing non-public host {request.url.host}")

        self._client = httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=6,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 6.0)),
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en;q=0.6",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            },
            limits=httpx.Limits(max_connections=global_concurrency * 2,
                                max_keepalive_connections=global_concurrency),
            event_hooks={"request": [_guard]},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _host_sem(self, host: str) -> asyncio.Semaphore:
        sem = self._host_sems.get(host)
        if sem is None:
            sem = asyncio.Semaphore(self._host_limits.get(host, self._per_host))
            self._host_sems[host] = sem
        return sem

    def global_remaining(self) -> float:
        if self.global_deadline is None:
            return 1e9
        return self.global_deadline - time.monotonic()

    async def get(self, url: str, *, ctx: Ctx | None = None, source_class: str = "web",
                  params: dict | None = None, headers: dict | None = None,
                  check_robots: bool = True, max_bytes: int | None = None,
                  timeout: float | None = None, keep: bool = True) -> FetchResult:
        res = FetchResult(url=url)
        if (ctx is not None and ctx.expired(1.0)) or self.global_remaining() < 2.0:
            res.error = "budget"
            return res
        try:
            parts = urlsplit(url)
        except ValueError:
            res.error = "bad_url"
            return res
        if parts.scheme not in ("http", "https") or not parts.hostname:
            res.error = "bad_url"
            return res
        if not safe_host(parts.hostname):
            res.error = "unsafe_url"
            return res
        if check_robots and self.respect_robots and not await self._robots_allowed(url, ctx):
            res.error = "robots"
            return res

        t = timeout or self.timeout
        if ctx is not None:
            t = max(1.0, min(t, ctx.remaining() - 0.5))
        t = max(1.0, min(t, self.global_remaining() - 1.0))
        host = parts.hostname.lower()
        limit = max_bytes or self.max_bytes

        async def _do():
            async with self._client.stream(
                "GET", url, params=params, headers=headers,
                timeout=httpx.Timeout(t, connect=min(t, 6.0)),
            ) as r:
                res.status = r.status_code
                res.final_url = str(r.url)
                res.redirects = [str(h.url) for h in r.history]
                res.content_type = r.headers.get("content-type", "")
                res.encoding = r.charset_encoding
                if 200 <= r.status_code < 300:
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > limit:
                            res.truncated = True
                            break
                    res.content = bytes(buf[:limit])

        async with self._global_sem, self._host_sem(host):
            self.request_count += 1
            if ctx is not None:
                ctx.requests += 1
            try:
                await asyncio.wait_for(_do(), timeout=t + 3.0)
            except (asyncio.TimeoutError, httpx.TimeoutException) as e:
                res.error, res.detail = "timeout", type(e).__name__
                self.host_errors[host] += 1
                return res
            except UnsafeRedirect as e:
                res.error, res.detail = "unsafe_url", str(e)[:160]
                return res
            except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError, ValueError, UnicodeError) as e:
                res.error, res.detail = "network", f"{type(e).__name__}: {str(e)[:160]}"
                self.host_errors[host] += 1
                return res

        self.bytes_in += len(res.content)
        s = res.status or 0
        if 200 <= s < 300:
            res.ok = True
            if keep and self.store is not None:
                res.snapshot = self.store.put(
                    content=res.content, requested_url=url, final_url=res.final_url or url,
                    status=s, content_type=res.content_type, redirects=res.redirects,
                    truncated=res.truncated, source_class=source_class,
                )
        elif s in (401, 403, 407, 429, 451, 999):
            res.error = "blocked"
        elif s in (404, 410):
            res.error = "not_found"
        else:
            res.error = "http_error"
        return res

    # ------------------------------------------------------------------ robots.txt
    async def _robots_allowed(self, url: str, ctx: Ctx | None) -> bool:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}".lower()
        fut = self._robots.get(origin)
        if fut is None or fut.cancelled():
            fut = asyncio.get_running_loop().create_future()
            self._robots[origin] = fut
            rp = RobotFileParser()
            transient = True
            try:
                r = await self.get(origin + "/robots.txt", ctx=ctx, source_class="robots",
                                   check_robots=False, keep=False, max_bytes=500_000, timeout=6)
                if r.ok and "html" not in (r.content_type or "").lower():
                    rp.parse(r.text.splitlines())
                    transient = False
                elif r.status and 500 <= r.status < 600:
                    rp.parse(["User-agent: *", "Disallow: /"])
                    transient = False
                else:
                    rp.parse([])
                    transient = r.error in ("budget", "timeout", "network")
            except Exception:
                rp.parse([])
            finally:
                if not rp.last_checked:
                    rp.parse([])
                if not fut.done():
                    fut.set_result(rp)
                if transient:
                    # Do not cache an allow-all decision made on a failed fetch.
                    if self._robots.get(origin) is fut:
                        self._robots.pop(origin, None)
        # shield: a cancelled waiter must not cancel the future other companies share
        rp = await asyncio.shield(fut)
        try:
            return rp.can_fetch(self.ua_token, url)
        except Exception:
            return True

    def sitemaps_for(self, url: str) -> list[str]:
        p = urlsplit(url)
        fut = self._robots.get(f"{p.scheme}://{p.netloc}".lower())
        if fut is not None and fut.done() and not fut.cancelled():
            try:
                return list(fut.result().site_maps() or [])
            except Exception:
                return []
        return []
