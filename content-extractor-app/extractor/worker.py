"""The extraction loop: claim URLs from n8n, fetch in parallel, send results back."""
from __future__ import annotations

import logging
import platform
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import urlparse

from .extract import ExtractError, Extractor
from .fetcher import SKIP_EXTENSIONS, DomainGate, FetchError, Fetcher, host_of
from .n8n import N8nAuthError, N8nClient, N8nError
from .renderer import RenderUnavailable, Renderer
from .stats import Stats

log = logging.getLogger(__name__)

try:
    import psutil
except ImportError:
    psutil = None

# Failures worth a second look in a real browser: the page told us it needs
# JavaScript, or it rendered to almost nothing. A hard refusal (403/429) is
# taken at face value and never retried.
RENDER_TRIGGERS = ("bot_challenge", "too_short")


class Limiter:
    """Concurrency limit that can be changed while tasks are running."""

    def __init__(self, limit: int) -> None:
        self._cv = threading.Condition()
        self._limit = max(1, int(limit))
        self._active = 0

    def set_limit(self, limit: int) -> None:
        with self._cv:
            self._limit = max(1, int(limit))
            self._cv.notify_all()

    @contextmanager
    def slot(self):
        with self._cv:
            while self._active >= self._limit:
                self._cv.wait(timeout=1.0)
            self._active += 1
        try:
            yield
        finally:
            with self._cv:
                self._active -= 1
                self._cv.notify()


def system_metrics() -> dict:
    metrics = {
        "server_host": socket.gethostname(),
        "server_os": platform.system(),
        "server_platform": platform.platform(),
    }
    if psutil is None:
        return metrics
    virtual_memory = psutil.virtual_memory()
    try:
        load1, load5, load15 = psutil.getloadavg()
    except (AttributeError, OSError):
        load1 = load5 = load15 = 0.0
    metrics.update({
        "cores_logical": psutil.cpu_count(logical=True) or 0,
        "cores_physical": psutil.cpu_count(logical=False) or 0,
        "cpu_percent": round(psutil.cpu_percent(interval=None), 2),
        "load_average_1m": round(load1, 2),
        "load_average_5m": round(load5, 2),
        "load_average_15m": round(load15, 2),
        "memory_total_bytes": virtual_memory.total,
        "memory_available_bytes": virtual_memory.available,
        "memory_used_percent": round(virtual_memory.percent, 2),
    })
    return metrics


class Worker:
    def __init__(self, settings, stats: Stats, client: N8nClient,
                 fetcher: Fetcher, gate: DomainGate, extractor: Extractor,
                 renderer: Renderer) -> None:
        self.settings = settings
        self.stats = stats
        self.client = client
        self.fetcher = fetcher
        self.gate = gate
        self.extractor = extractor
        self.renderer = renderer

        self.limiter = Limiter(settings.threads)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- control
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread:
            thread.join(timeout=timeout)

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            problems = self.settings.problems()
            if problems:
                self.stats.set_state("error", problems[0])
                log.error("Refusing to start - %s", problems[0])
                return False

            self._stop.clear()
            self._paused.clear()
            self._pool = ThreadPoolExecutor(
                max_workers=self.settings.max_threads, thread_name_prefix="fetch"
            )
            self._thread = threading.Thread(target=self._loop, name="worker", daemon=True)
            self._thread.start()
            if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop, name="heartbeat", daemon=True
                )
                self._heartbeat_thread.start()
            log.info("Worker started (node_name=%s)", self.settings.node_name)
            return True

    def stop(self, wait: float = 90.0) -> None:
        """Stop after the current batch so no claimed URL is dropped mid-flight."""
        if not self.running:
            self.stats.set_state("stopped")
            return
        log.info("Stopping worker - finishing the current batch first")
        self.stats.set_state("stopping")
        self._stop.set()
        self._paused.clear()
        thread = self._thread
        if thread:
            thread.join(timeout=wait)
        pool, self._pool = self._pool, None
        if pool:
            pool.shutdown(wait=False)
        self.stats.set_state("stopped")

    def pause(self) -> None:
        if self.running:
            self._paused.set()
            self.stats.set_state("paused")
            log.info("Worker paused")

    def resume(self) -> None:
        if self.running and self._paused.is_set():
            self._paused.clear()
            self.stats.set_state("running")
            log.info("Worker resumed")

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep - a stop request never waits out the timer."""
        self._stop.wait(timeout=max(seconds, 0))

    # ---------------------------------------------------------------- loop
    def _loop(self) -> None:
        self.stats.set_state("running")
        backoff = 5

        while not self._stop.is_set():
            if self._paused.is_set():
                self._sleep(1.0)
                continue

            if self.settings.max_hours and \
                    self.stats.uptime_seconds() >= self.settings.max_hours * 3600:
                log.info("MAX_HOURS reached - stopping")
                self.stats.set_state("stopped", "max_hours reached")
                break

            cycle_started = time.monotonic()

            # --- claim -------------------------------------------------
            try:
                records = self.client.claim(self.settings.batch_size)
                backoff = 5
            except N8nAuthError as exc:
                self.stats.set_state("error", str(exc))
                log.error("Authentication failed - fix N8N_API_KEY, then start again")
                break
            except N8nError as exc:
                self.stats.set_state("error", str(exc))
                log.error("Could not claim a batch: %s (retrying in %ss)", exc, backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue

            if not records:
                self.stats.record_cycle(time.monotonic() - cycle_started, empty=True)
                if self.settings.max_empty_cycles and \
                        self.stats.empty_cycles >= self.settings.max_empty_cycles:
                    log.info("Queue empty for %d cycles - stopping", self.stats.empty_cycles)
                    self.stats.set_state("stopped", "queue empty")
                    break
                self.stats.set_state("idle", "queue empty")
                log.info("Queue empty - next check in %ss", self.settings.idle_sleep_seconds)
                self._sleep(self.settings.idle_sleep_seconds)
                continue

            self.stats.set_state("running")
            self.stats.start_batch(len(records))
            self.limiter.set_limit(self.settings.threads)

            # --- fetch + extract in parallel ---------------------------
            pool = self._pool
            if pool is None:
                break
            results = list(pool.map(self._process, records))

            # --- send back ---------------------------------------------
            to_save = [r for r in results if not r.get("requeue")]
            requeued = len(results) - len(to_save)
            saved = None
            if to_save:
                try:
                    saved = self.client.save(to_save)
                    self.stats.record_save(True)
                except N8nAuthError as exc:
                    self.stats.set_state("error", str(exc))
                    log.error("Authentication failed while saving - stopping")
                    break
                except N8nError as exc:
                    self.stats.record_save(False)
                    log.error(
                        "Batch not saved (%s) - n8n will requeue these %d URLs",
                        exc, len(to_save),
                    )

            ok = sum(1 for r in results if r["status"] == "content")
            elapsed = time.monotonic() - cycle_started
            self.stats.record_cycle(elapsed, empty=False)
            log.info(
                "cycle %d | ok %d | fail %d%s | %.1fs | saved=%s",
                self.stats.cycles, ok, len(results) - ok,
                f" | requeue {requeued}" if requeued else "",
                elapsed,
                (saved or {}).get("saved", "-"),
            )

            if requeued == len(results):
                # Everything we claimed was on a cooling-off host: back off
                # instead of spinning through the queue.
                self._sleep(self.settings.idle_sleep_seconds)

        if self.stats.state not in ("error", "stopped"):
            self.stats.set_state("stopped")
        self._send_heartbeat()

    # ---------------------------------------------------------------- one URL
    def _process(self, record: dict) -> dict:
        started = time.monotonic()
        url = (record.get("url") or "").strip()
        result = {
            "id": record.get("id"),
            "url": url,
            "host": host_of(url),
            "status": "error",
            "content": None,
            "chars": 0,
            "error": None,
            "http_status": None,
            "method": None,
            "via": None,
            "elapsed_ms": 0,
            "requeue": False,
        }
        self.stats.task_started()
        try:
            with self.limiter.slot():
                if self._stop.is_set():
                    # Shutting down: leave it claimed, n8n requeues it.
                    result["error"] = "worker_stopping"
                    result["requeue"] = True
                    return result
                self._fetch_and_extract(url, result)
        except FetchError as exc:
            result["error"] = exc.reason
            result["http_status"] = result["http_status"] or exc.http_status
        except ExtractError as exc:
            result["error"] = exc.reason
        except Exception as exc:  # never let one URL kill the batch
            result["error"] = f"{type(exc).__name__}:{str(exc)[:100]}"
            log.debug("Unexpected error on %s", url, exc_info=True)
        finally:
            result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            if result["status"] != "content" and not result["error"]:
                result["error"] = "unknown"
            self.stats.record_result(result)

        host = result["host"]
        if host:
            self.gate.record(host, ok=result["status"] == "content",
                             http_status=result["http_status"])
        return result

    def _fetch_and_extract(self, url: str, result: dict) -> None:
        if not url.lower().startswith(("http://", "https://")):
            raise FetchError("bad_url")

        path = urlparse(url).path.lower()
        if path.endswith(SKIP_EXTENSIONS):
            raise FetchError("unsupported_file_type")

        host = result["host"]
        cooling = self.gate.cooling_for(host)
        if cooling > 0:
            # This host is blocking us right now. Do not spend a request on it
            # and do not mark the row failed - leave it claimed so n8n requeues
            # it once the cool-off has passed.
            result["requeue"] = True
            raise FetchError(f"domain_cooling:{int(cooling)}s")

        self.gate.wait(host)
        response = self.fetcher.fetch(url)
        result["http_status"] = response.http_status
        result["via"] = response.via

        try:
            content, method = self.extractor.extract(response.html, url)
        except ExtractError as exc:
            if not self._should_render(exc.reason):
                raise
            content, method = self._render_and_extract(url, result, exc)

        result.update(status="content", content=content, chars=len(content), method=method)

    def _should_render(self, reason: str) -> bool:
        return (
            self.settings.render_fallback
            and reason.split(":")[0] in RENDER_TRIGGERS
        )

    def _render_and_extract(self, url: str, result: dict, original: ExtractError):
        """Second attempt in a real browser, for pages that need JavaScript."""
        try:
            html = self.renderer.render(url)
        except RenderUnavailable as exc:
            log.debug("Render fallback unavailable for %s: %s", url, exc)
            raise original from None

        result["via"] = "render"
        content, method = self.extractor.extract(html, url)
        return content, f"{method}+render"

    # ---------------------------------------------------------------- test
    def test_url(self, url: str) -> dict:
        """Fetch + extract one URL without touching the queue or the database."""
        started = time.monotonic()
        result = {
            "id": None, "url": url, "host": host_of(url), "status": "error",
            "content": None, "chars": 0, "error": None, "http_status": None,
            "method": None, "via": None, "elapsed_ms": 0, "requeue": False,
        }
        try:
            self._fetch_and_extract(url, result)
        except (FetchError, ExtractError) as exc:
            result["error"] = exc.reason
            result["http_status"] = result["http_status"] or getattr(exc, "http_status", None)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}:{str(exc)[:120]}"
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        result["preview"] = (result["content"] or "")[:1200]
        result.pop("content", None)
        return result

    # ---------------------------------------------------------------- heartbeat
    def _send_heartbeat(self) -> None:
        payload = self.stats.heartbeat_payload(self.settings)
        payload.update(system_metrics())
        payload["renderer"] = self.renderer.snapshot()
        try:
            self.client.heartbeat(payload)
        except N8nAuthError:
            log.error("Heartbeat rejected: bad N8N_API_KEY")
        except Exception as exc:
            log.debug("Heartbeat failed: %s", exc)

    def _heartbeat_loop(self) -> None:
        while True:
            self._send_heartbeat()
            interval = max(int(self.settings.heartbeat_seconds), 15)
            if self._stop.wait(timeout=interval):
                # Let the loop settle on its final state, report it, and exit.
                time.sleep(1.0)
                self._send_heartbeat()
                return
