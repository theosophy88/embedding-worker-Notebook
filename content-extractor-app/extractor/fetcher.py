"""HTTP fetching: pooled HTTP/2 clients, per-domain pacing, per-domain breaker."""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

HEADER_CHARSET_RE = re.compile(r"charset=[\"']?([\w\-]+)", re.I)
META_CHARSET_RE = re.compile(rb"charset=[\"']?([\w\-]+)", re.I)

# Statuses that mean "this host is refusing bots", not "this page is broken".
BLOCK_STATUSES = (401, 403, 405, 406, 409, 418, 429, 503)

SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".mp4", ".mp3", ".wav", ".avi", ".mov", ".zip", ".gz", ".rar",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".json",
)

HTML_CONTENT_TYPES = ("html", "xml", "text/plain")


class FetchError(Exception):
    """Unusable response. `reason` is short and machine-readable."""

    def __init__(self, reason: str, http_status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


@dataclass
class DomainState:
    ok: int = 0
    failed: int = 0
    blocked: int = 0
    consecutive_blocks: int = 0
    cooling_until: float = 0.0
    next_free: float = 0.0
    last_status: int | None = None


@dataclass
class FetchResult:
    html: str
    http_status: int
    via: str = "http"
    size: int = 0


def host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except ValueError:
        return ""


def decode_body(raw: bytes, content_type: str) -> str:
    """Charset from the header, else from the <meta> tag, else utf-8."""
    match = HEADER_CHARSET_RE.search(content_type or "")
    encoding = match.group(1) if match else None
    if not encoding:
        meta = META_CHARSET_RE.search(raw[:4096])
        if meta:
            encoding = meta.group(1).decode("ascii", "ignore")
    try:
        return raw.decode(encoding or "utf-8", errors="replace")
    except (LookupError, TypeError):
        return raw.decode("utf-8", errors="replace")


class DomainGate:
    """Paces requests per host and cools off hosts that keep blocking us.

    Two jobs, both about being a good citizen and not wasting requests:
      * never hit one host more often than `per_domain_delay`
      * after N consecutive blocks (403/429/...), stop asking that host for a while
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._hosts: dict[str, DomainState] = {}

    def _state(self, host: str) -> DomainState:
        state = self._hosts.get(host)
        if state is None:
            state = DomainState()
            self._hosts[host] = state
        return state

    def cooling_for(self, host: str) -> float:
        """Seconds left on this host's cool-off, or 0 if it is available."""
        with self._lock:
            remaining = self._state(host).cooling_until - time.monotonic()
        return max(remaining, 0.0)

    def wait(self, host: str) -> None:
        """Block until this host may be hit again."""
        while True:
            with self._lock:
                state = self._state(host)
                now = time.monotonic()
                if now >= state.next_free:
                    state.next_free = now + self.settings.per_domain_delay
                    return
                wait = state.next_free - now
            time.sleep(min(wait, 2.0))

    def record(self, host: str, *, ok: bool, http_status: int | None = None) -> None:
        blocked = http_status in BLOCK_STATUSES
        with self._lock:
            state = self._state(host)
            state.last_status = http_status
            if ok:
                state.ok += 1
                state.consecutive_blocks = 0
                return

            state.failed += 1
            if not blocked:
                state.consecutive_blocks = 0
                return

            state.blocked += 1
            state.consecutive_blocks += 1
            threshold = self.settings.domain_failure_threshold
            if threshold and state.consecutive_blocks >= threshold:
                state.cooling_until = time.monotonic() + self.settings.domain_cooloff_seconds
                state.consecutive_blocks = 0
                log.warning(
                    "Cooling off %s for %ss after %d blocks",
                    host, self.settings.domain_cooloff_seconds, threshold,
                )

    def snapshot(self, limit: int = 15) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            rows = [
                {
                    "host": host,
                    "ok": state.ok,
                    "failed": state.failed,
                    "blocked": state.blocked,
                    "last_status": state.last_status,
                    "cooling_seconds": max(round(state.cooling_until - now), 0),
                }
                for host, state in self._hosts.items()
            ]
        rows.sort(key=lambda row: -(row["ok"] + row["failed"]))
        return rows[:limit]

    def reset_cooloffs(self) -> int:
        with self._lock:
            count = 0
            for state in self._hosts.values():
                if state.cooling_until:
                    state.cooling_until = 0.0
                    state.consecutive_blocks = 0
                    count += 1
        return count


class Fetcher:
    """One pooled httpx client per thread, so connections are reused."""

    def __init__(self, settings, gate: DomainGate) -> None:
        self.settings = settings
        self.gate = gate
        self._local = threading.local()
        self._clients: list[httpx.Client] = []
        self._clients_lock = threading.Lock()

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        }

    def client(self) -> httpx.Client:
        client = getattr(self._local, "client", None)
        if client is None:
            kwargs = {
                "headers": self._headers(),
                "follow_redirects": True,
                "verify": self.settings.verify_tls,
                "timeout": httpx.Timeout(
                    connect=self.settings.connect_timeout,
                    read=self.settings.read_timeout,
                    write=15.0, pool=15.0,
                ),
                "limits": httpx.Limits(max_connections=8, max_keepalive_connections=4),
                "trust_env": False,
            }
            if self.settings.proxy_url:
                kwargs["proxy"] = self.settings.proxy_url
            try:
                client = httpx.Client(http2=self.settings.http2, **kwargs)
            except ImportError:
                # httpx raises this when http2=True but the h2 package is absent
                log.warning("HTTP/2 unavailable (h2 not installed) - using HTTP/1.1")
                client = httpx.Client(http2=False, **kwargs)
            self._local.client = client
            with self._clients_lock:
                self._clients.append(client)
        return client

    def fetch(self, url: str) -> FetchResult:
        """Download one page. Raises FetchError for anything unusable."""
        try:
            with self.client().stream("GET", url) as response:
                status = response.status_code
                if status >= 400:
                    raise FetchError(f"http_{status}", status)

                content_type = (response.headers.get("content-type") or "").lower()
                if content_type and not any(t in content_type for t in HTML_CONTENT_TYPES):
                    raise FetchError(f"not_html:{content_type.split(';')[0]}", status)

                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.settings.max_page_bytes:
                    raise FetchError("too_large", status)

                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.settings.max_page_bytes:
                        raise FetchError("too_large", status)

                body = decode_body(b"".join(chunks), content_type)
                return FetchResult(html=body, http_status=status, via="http", size=size)

        except FetchError:
            raise
        except httpx.TooManyRedirects:
            raise FetchError("too_many_redirects") from None
        except httpx.ConnectTimeout:
            raise FetchError("connect_timeout") from None
        except httpx.ReadTimeout:
            raise FetchError("read_timeout") from None
        except httpx.TimeoutException:
            raise FetchError("timeout") from None
        except httpx.ProxyError as exc:
            raise FetchError(f"proxy_error:{str(exc)[:60]}") from None
        except httpx.ConnectError as exc:
            text = str(exc).lower()
            if "certificate" in text or "ssl" in text:
                raise FetchError("ssl_error") from None
            if "name or service not known" in text or "nodename" in text:
                raise FetchError("dns_error") from None
            raise FetchError("connection_error") from None
        except httpx.RemoteProtocolError:
            raise FetchError("protocol_error") from None
        except httpx.HTTPError as exc:
            raise FetchError(f"http_error:{type(exc).__name__}") from None
        except UnicodeError:
            raise FetchError("bad_url") from None

    def close(self) -> None:
        with self._clients_lock:
            clients, self._clients = self._clients, []
            # A fresh threading.local drops every thread's reference to the
            # closed clients, so a later fetch builds a new one instead of
            # failing with "client has been closed".
            self._local = threading.local()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass
