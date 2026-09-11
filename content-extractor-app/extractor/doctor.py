"""Pre-flight checks: is this machine able to do the job at all?

Every check returns a verdict plus a *fix*, so a failure tells you what to do
rather than just that something is wrong.
"""
from __future__ import annotations

import socket
import sys
from dataclasses import dataclass
from importlib import import_module
from urllib.parse import urlparse

OK, WARN, FAIL = "ok", "warn", "fail"

PROBE_URLS = ("https://example.com", "https://www.wikipedia.org")


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "fix": self.fix}


def check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info < (3, 9):
        return Check("Python version", FAIL, f"{version} is too old",
                     "Install Python 3.9 or newer (3.11+ recommended)")
    return Check("Python version", OK, version)


def check_packages() -> list[Check]:
    required = {
        "httpx": "HTTP client",
        "trafilatura": "article extraction",
        "fastapi": "control panel",
        "uvicorn": "control panel server",
    }
    optional = {"psutil": "server metrics in the heartbeat",
                "h2": "HTTP/2 support",
                "playwright": "headless-browser fallback"}
    checks = []
    for module, purpose in required.items():
        try:
            loaded = import_module(module)
            version = getattr(loaded, "__version__", "?")
            checks.append(Check(f"Package {module}", OK, f"{version} ({purpose})"))
        except ImportError:
            checks.append(Check(f"Package {module}", FAIL, f"missing ({purpose})",
                                "Run: pip install -r requirements.txt"))
    for module, purpose in optional.items():
        try:
            import_module(module)
            checks.append(Check(f"Package {module}", OK, f"present ({purpose})"))
        except ImportError:
            checks.append(Check(f"Package {module}", WARN, f"not installed ({purpose})",
                                f"Optional. pip install {module}"))
    return checks


def check_config(settings) -> list[Check]:
    path = getattr(settings, "config_path", None)
    checks = [
        Check("Config file", OK if path else WARN,
              path or "none found - using defaults and environment variables",
              "" if path else "Create /etc/content-extractor/config.env")
    ]
    problems = settings.problems()
    if problems:
        checks.append(Check("Config values", FAIL, "; ".join(problems),
                            "Edit the config file, then restart the service"))
    else:
        checks.append(Check("Config values", OK,
                            f"node_name={settings.node_name} "
                            f"batch={settings.batch_size} threads={settings.threads}"))

    # Settings that could not be parsed fell back to defaults - the worker runs,
    # just not the way the file says, which is worth failing loudly over.
    ignored = list(getattr(settings, "ignored_keys", []))
    if ignored:
        checks.append(Check(
            "Config parsing", FAIL,
            f"ignored, using defaults instead: {', '.join(ignored)}",
            "Most likely an inline '# comment' after the value - systemd keeps it "
            "as part of the value. Put comments on their own line, then restart",
        ))
    return checks


def check_dns(settings) -> Check:
    host = urlparse(settings.base_url).hostname
    if not host:
        return Check("DNS", FAIL, f"cannot read a hostname from {settings.base_url}",
                     "Fix N8N_BASE_URL")
    try:
        address = socket.gethostbyname(host)
        return Check("DNS", OK, f"{host} -> {address}")
    except OSError as exc:
        return Check("DNS", FAIL, f"{host} does not resolve ({exc})",
                     "Check the server's DNS settings (/etc/resolv.conf) and the URL")


def check_egress() -> Check:
    """Can this box reach the open web at all?"""
    try:
        import httpx
    except ImportError:
        return Check("Internet egress", FAIL, "httpx missing",
                     "Run: pip install -r requirements.txt")

    errors = []
    for url in PROBE_URLS:
        try:
            response = httpx.get(url, timeout=15, follow_redirects=True,
                                 headers={"User-Agent": "content-extractor/doctor"})
            if response.status_code < 400:
                return Check("Internet egress", OK, f"{url} -> HTTP {response.status_code}")
            errors.append(f"{url} -> HTTP {response.status_code}")
        except Exception as exc:
            errors.append(f"{url} -> {type(exc).__name__}")
    return Check("Internet egress", FAIL, "; ".join(errors),
                 "No outbound HTTPS. Check the firewall, NAT or proxy settings")


def check_n8n(settings) -> Check:
    """Reach the API and prove the key works, without consuming any queue rows."""
    if not settings.api_key:
        return Check("n8n API", FAIL, "no API key configured",
                     "Set N8N_API_KEY to the key used in the n8n Auth nodes")
    try:
        from .n8n import N8nAuthError, N8nClient, N8nError
    except ImportError as exc:
        return Check("n8n API", FAIL, str(exc), "Reinstall dependencies")

    client = N8nClient(settings)
    try:
        ok = client.heartbeat({
            "node_name": settings.node_name,
            "worker_type": settings.worker_type,
            "status": "doctor",
            "app": "content-extractor-app",
        })
        if ok:
            return Check("n8n API", OK, f"{settings.status_url} accepted the heartbeat")
        return Check("n8n API", FAIL, client.last_error or "no response",
                     "Is the workflow Active in n8n? Check the URL and reverse proxy")
    except N8nAuthError:
        return Check("n8n API", FAIL, "401 unauthorized",
                     "N8N_API_KEY does not match the key in the n8n Auth nodes")
    except N8nError as exc:
        return Check("n8n API", FAIL, str(exc)[:200],
                     "Check N8N_BASE_URL and that the workflow is Active")
    finally:
        client.close()


def check_panel_port(settings) -> Check:
    """A port already in use is the classic 'service starts then dies' cause."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        if probe.connect_ex(("127.0.0.1", settings.panel_port)) == 0:
            return Check("Panel port", WARN,
                         f"port {settings.panel_port} is already in use",
                         "Either the service is already running, or pick another PANEL_PORT")
    return Check("Panel port", OK, f"{settings.panel_host}:{settings.panel_port} is free")


def check_renderer(settings) -> Check:
    from .renderer import playwright_available

    available, detail = playwright_available()
    if not settings.render_fallback:
        return Check("Headless renderer", OK, "disabled (RENDER_FALLBACK=false)")
    if not available:
        return Check("Headless renderer", FAIL, detail,
                     "Run: pip install playwright && playwright install --with-deps chromium")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            browser.close()
        return Check("Headless renderer", OK, "chromium launches")
    except Exception as exc:
        return Check("Headless renderer", FAIL, f"{type(exc).__name__}: {str(exc)[:140]}",
                     "Run: playwright install --with-deps chromium")


def run_checks(settings, *, include_network: bool = True) -> list[Check]:
    checks = [check_python(), *check_packages(), *check_config(settings)]
    if include_network:
        checks.append(check_dns(settings))
        checks.append(check_egress())
        checks.append(check_n8n(settings))
    checks.append(check_panel_port(settings))
    checks.append(check_renderer(settings))
    return checks


def print_checks(checks: list[Check]) -> int:
    symbols = {OK: "\033[32m✓\033[0m", WARN: "\033[33m!\033[0m", FAIL: "\033[31m✗\033[0m"}
    width = max(len(check.name) for check in checks)
    print()
    for check in checks:
        print(f"  {symbols[check.status]} {check.name.ljust(width)}  {check.detail}")
        if check.fix and check.status != OK:
            print(f"    {' ' * width}  -> {check.fix}")
    failures = sum(1 for check in checks if check.status == FAIL)
    warnings = sum(1 for check in checks if check.status == WARN)
    print()
    if failures:
        print(f"  {failures} check(s) failed, {warnings} warning(s). "
              "Fix the failures above before starting the worker.")
    else:
        print(f"  All checks passed ({warnings} warning(s)). Ready to run.")
    print()
    return 1 if failures else 0
