"""Command line: `python -m extractor [run|doctor|test|config]`."""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import secrets
import signal
import sys
import threading

from . import __version__
from .config import Settings
from .doctor import print_checks, run_checks
from .extract import Extractor
from .fetcher import DomainGate, Fetcher
from .logbuf import setup_logging
from .n8n import N8nClient
from .renderer import Renderer
from .stats import Stats
from .worker import Worker

log = logging.getLogger("extractor")


class Context:
    """Everything the worker and the panel share."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stats = Stats()
        self.client = N8nClient(settings)
        self.gate = DomainGate(settings)
        self.fetcher = Fetcher(settings, self.gate)
        self.extractor = Extractor(settings)
        self.renderer = Renderer(settings)
        self.worker = Worker(settings, self.stats, self.client, self.fetcher,
                             self.gate, self.extractor, self.renderer)

    def shutdown(self) -> None:
        self.worker.stop()
        self.renderer.stop()
        self.fetcher.close()
        self.client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="content-extractor",
        description="News content extraction worker for the n8n Content Extractor API.",
    )
    parser.add_argument("--version", action="version", version=f"content-extractor {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the worker and the control panel (default)")
    run.add_argument("--no-panel", action="store_true", help="worker only, no web panel")
    run.add_argument("--no-worker", action="store_true", help="panel only, do not start extracting")

    doctor = sub.add_parser("doctor", help="check this machine, the config and the n8n API")
    doctor.add_argument("--offline", action="store_true", help="skip the network checks")

    test = sub.add_parser("test", help="extract one URL and print the result")
    test.add_argument("url")
    test.add_argument("--full", action="store_true", help="print the whole extracted text")

    sub.add_parser("config", help="print the effective configuration")
    return parser


def cmd_doctor(settings: Settings, args) -> int:
    checks = run_checks(settings, include_network=not args.offline)
    return print_checks(checks)


def cmd_test(settings: Settings, args) -> int:
    context = Context(settings)
    url = args.url if args.url.startswith(("http://", "https://")) else "https://" + args.url
    try:
        result = context.worker.test_url(url)
    finally:
        context.renderer.stop()
        context.fetcher.close()

    print()
    print(f"  url      : {url}")
    print(f"  status   : {result['status']}")
    print(f"  http     : {result['http_status']}")
    print(f"  via      : {result['via']}   method: {result['method']}")
    print(f"  chars    : {result['chars']}")
    print(f"  elapsed  : {result['elapsed_ms']} ms")
    print(f"  error    : {result['error'] or 'none'}")
    print("  " + "-" * 70)
    preview = result.get("preview") or "(no text extracted)"
    print(preview if args.full else preview[:600])
    print()
    return 0 if result["status"] == "content" else 1


def cmd_config(settings: Settings) -> int:
    print(json.dumps(settings.public(), indent=2, default=str))
    problems = settings.problems()
    if problems:
        print("\nProblems:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


def cmd_run(settings: Settings, args) -> int:
    context = Context(settings)
    problems = settings.problems()
    for problem in problems:
        log.error("Config problem: %s", problem)

    if settings.render_fallback:
        if context.renderer.start():
            log.info("Headless-browser fallback is on")
        else:
            log.warning("Headless-browser fallback requested but unavailable: %s",
                        context.renderer.last_error)

    if not args.no_worker and settings.autostart and not problems:
        context.worker.start()
    elif problems:
        log.error("Worker not started - fix the config above, then press Start in the panel")
        context.stats.set_state("error", problems[0])
    else:
        context.stats.set_state("stopped", "autostart disabled")

    stopping = threading.Event()

    def handle_signal(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        log.info("Received %s - shutting down", signal.Signals(signum).name)
        context.shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
            pass  # not the main thread, or platform without this signal

    if args.no_panel:
        log.info("Panel disabled - running worker only")
        try:
            while context.worker.running:
                context.worker.join(timeout=1.0)
        except KeyboardInterrupt:
            pass
        finally:
            if not stopping.is_set():
                context.shutdown()
        return 0

    if not settings.panel_password:
        settings.panel_password = secrets.token_urlsafe(12)
        log.warning("PANEL_PASSWORD was not set - generated one for this run:")
        log.warning("    user: %s   password: %s", settings.panel_user, settings.panel_password)

    import uvicorn

    from .panel import create_app

    app = create_app(context)
    log.info("Panel on http://%s:%s (user %s)",
             settings.panel_host, settings.panel_port, settings.panel_user)

    try:
        uvicorn.run(app, host=settings.panel_host, port=settings.panel_port,
                    log_level="warning", access_log=False)
    except OSError as exc:
        log.error("Cannot bind %s:%s - %s", settings.panel_host, settings.panel_port, exc)
        context.shutdown()
        return 2
    finally:
        if not stopping.is_set():
            context.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"
    if command == "run" and not hasattr(args, "no_panel"):
        # bare `python -m extractor` - fill in the run defaults
        args = parser.parse_args(["run"])

    settings = Settings.load()
    setup_logging(settings.log_level)
    log.info("content-extractor %s starting (node_name=%s, config=%s, app=%s)",
             __version__, settings.node_name,
             getattr(settings, "config_path", None) or "defaults+env",
             pathlib.Path(__file__).resolve().parent)

    if command == "doctor":
        return cmd_doctor(settings, args)
    if command == "test":
        return cmd_test(settings, args)
    if command == "config":
        return cmd_config(settings)
    return cmd_run(settings, args)
