"""Optional headless-browser fallback.

Some news sites serve an empty shell to plain HTTP clients and build the
article with JavaScript. Rendering the page in a real browser is the honest way
to read those - we run the site's own code instead of pretending to be someone
we are not.

Playwright's sync API is not thread-safe, so one dedicated thread owns the
browser and serves render requests from a queue. Rendering is slow by design
(one page at a time); it is a fallback for the pages plain HTTP cannot read,
not the main path.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class _Job:
    url: str
    timeout: float
    done: threading.Event
    html: str | None = None
    error: str | None = None


class RenderUnavailable(RuntimeError):
    pass


def playwright_available() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False, "playwright package not installed"
    return True, "playwright importable"


class Renderer:
    """Serial headless-Chromium renderer. `start()` is lazy and safe to re-call."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self.status = "stopped"
        self.last_error: str | None = None
        self.rendered = 0
        self.failed = 0

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return True
            ok, detail = playwright_available()
            if not ok:
                self.status = "unavailable"
                self.last_error = detail
                return False
            self._ready.clear()
            self.status = "starting"
            self._thread = threading.Thread(target=self._loop, name="renderer", daemon=True)
            self._thread.start()
        # Chromium takes a moment to come up; the worker can wait for it once.
        self._ready.wait(timeout=60)
        return self.status == "ready"

    def stop(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
        if thread and thread.is_alive():
            self._queue.put(None)
            thread.join(timeout=20)
        self.status = "stopped"

    def render(self, url: str) -> str:
        """Render one page and return its HTML. Raises RenderUnavailable."""
        if not self.running and not self.start():
            raise RenderUnavailable(self.last_error or "renderer unavailable")

        job = _Job(url=url, timeout=self.settings.render_timeout, done=threading.Event())
        self._queue.put(job)
        # Generous margin over the page timeout, plus queue wait.
        if not job.done.wait(timeout=self.settings.render_timeout * 3 + 30):
            raise RenderUnavailable("render timed out in queue")
        if job.error:
            raise RenderUnavailable(job.error)
        return job.html or ""

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage",
                          "--disable-gpu", "--disable-extensions"],
                )
                self.status = "ready"
                self._ready.set()
                log.info("Headless renderer ready")

                try:
                    while True:
                        job = self._queue.get()
                        if job is None:
                            break
                        self._render_one(browser, job, PlaywrightError)
                finally:
                    browser.close()

        except Exception as exc:
            self.status = "error"
            self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.error("Renderer stopped: %s", self.last_error)
            self._ready.set()
            # Fail anything still queued rather than letting callers hang.
            while True:
                try:
                    pending = self._queue.get_nowait()
                except queue.Empty:
                    break
                if pending is not None:
                    pending.error = self.last_error
                    pending.done.set()
        finally:
            if self.status not in ("error", "unavailable"):
                self.status = "stopped"

    def _render_one(self, browser, job: _Job, playwright_error) -> None:
        context = None
        try:
            context = browser.new_context(
                user_agent=self.settings.user_agent,
                locale="en-US",
                viewport={"width": 1366, "height": 900},
                ignore_https_errors=not self.settings.verify_tls,
                proxy={"server": self.settings.proxy_url} if self.settings.proxy_url else None,
            )
            page = context.new_page()
            page.goto(job.url, timeout=job.timeout * 1000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except playwright_error:
                pass  # networkidle never arrives on pages with polling widgets
            job.html = page.content()
            self.rendered += 1
        except playwright_error as exc:
            job.error = f"render_failed:{str(exc).splitlines()[0][:120]}"
            self.failed += 1
        except Exception as exc:
            job.error = f"render_failed:{type(exc).__name__}"
            self.failed += 1
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            job.done.set()

    def snapshot(self) -> dict:
        return {
            "enabled": bool(self.settings.render_fallback),
            "status": self.status,
            "rendered": self.rendered,
            "failed": self.failed,
            "last_error": self.last_error,
        }
