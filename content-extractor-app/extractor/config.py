"""Configuration.

Values come from (in order of precedence):
  1. real environment variables
  2. the config file  (/etc/content-extractor/config.env, or $EXTRACTOR_CONFIG)
  3. the defaults below
"""
from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

CONFIG_SEARCH_PATHS = (
    "/etc/content-extractor/config.env",
    "./config.env",
)

# Fields the panel is allowed to change while the worker is running.
LIVE_TUNABLE = frozenset({
    "batch_size", "threads", "per_domain_delay", "connect_timeout",
    "read_timeout", "min_content_chars", "max_content_chars",
    "strip_punctuation", "idle_sleep_seconds", "render_fallback",
    "domain_failure_threshold", "domain_cooloff_seconds",
})

# field name -> env var name (anything not listed uses the upper-cased field name)
ENV_ALIASES = {
    "api_key": "N8N_API_KEY",
    "base_url": "N8N_BASE_URL",
}


def find_config_file() -> Path | None:
    explicit = os.environ.get("EXTRACTOR_CONFIG")
    candidates = [explicit] if explicit else list(CONFIG_SEARCH_PATHS)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def load_config_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file. Comments, blanks and `export ` prefixes are fine."""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip().upper()] = value
    return values


def _coerce(value: Any, kind: Any) -> Any:
    if kind is bool or kind == "bool":
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind is int or kind == "int":
        return int(float(value))
    if kind is float or kind == "float":
        return float(value)
    return str(value)


@dataclass
class Settings:
    # --- n8n API -----------------------------------------------------
    base_url: str = "https://n8n.3rfan.ir/webhook"
    api_key: str = ""
    node_name: str = field(default_factory=socket.gethostname)
    worker_type: str = "content-extractor"

    # --- throughput --------------------------------------------------
    batch_size: int = 60
    threads: int = 24
    max_threads: int = 64          # hard ceiling; the thread pool is built this big

    # --- fetching ----------------------------------------------------
    connect_timeout: float = 10.0
    read_timeout: float = 25.0
    per_domain_delay: float = 1.0  # min seconds between two hits on one host
    max_page_bytes: int = 3_000_000
    user_agent: str = CHROME_UA
    proxy_url: str = ""            # e.g. http://user:pass@host:3128
    verify_tls: bool = True
    http2: bool = True

    # --- per-domain circuit breaker ----------------------------------
    domain_failure_threshold: int = 5     # consecutive blocks before cooling off
    domain_cooloff_seconds: int = 900

    # --- optional headless-browser fallback --------------------------
    render_fallback: bool = False
    render_timeout: float = 30.0

    # --- content rules (must match the n8n Normalize Results node) ----
    min_content_chars: int = 150
    max_content_chars: int = 10_000
    strip_punctuation: bool = True

    # --- loop behaviour ----------------------------------------------
    idle_sleep_seconds: int = 30
    max_empty_cycles: int = 0      # 0 = keep polling forever
    max_hours: float = 0.0         # 0 = run forever
    heartbeat_seconds: int = 60
    autostart: bool = True

    # --- panel -------------------------------------------------------
    panel_host: str = "127.0.0.1"
    panel_port: int = 8787
    panel_user: str = "admin"
    panel_password: str = ""       # empty -> one is generated and logged at startup

    # --- misc --------------------------------------------------------
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    @classmethod
    def load(cls) -> "Settings":
        file_values: dict[str, str] = {}
        path = find_config_file()
        if path:
            try:
                file_values = load_config_file(path)
            except OSError as exc:
                log.warning("Could not read config file %s: %s", path, exc)
        settings = cls()
        settings.config_path = str(path) if path else None

        for f in fields(cls):
            env_name = ENV_ALIASES.get(f.name, f.name.upper())
            raw = os.environ.get(env_name, file_values.get(env_name))
            if raw is None or raw == "":
                continue
            try:
                setattr(settings, f.name, _coerce(raw, f.type))
            except (TypeError, ValueError):
                log.warning("Ignoring invalid value for %s: %r", env_name, raw)
        return settings

    # ------------------------------------------------------------------
    @property
    def get_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/content-get-batch"

    @property
    def save_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/content-save"

    @property
    def status_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/content-status"

    def problems(self) -> list[str]:
        """Configuration errors that must be fixed before the worker can run."""
        issues = []
        if not self.base_url.startswith(("http://", "https://")):
            issues.append("N8N_BASE_URL must start with http:// or https://")
        if "/webhook" not in self.base_url:
            issues.append(
                "N8N_BASE_URL usually ends in /webhook "
                "(e.g. https://n8n.example.com/webhook)"
            )
        if not self.api_key:
            issues.append("N8N_API_KEY is empty - set it to the key in the n8n Auth nodes")
        if not 1 <= self.batch_size <= 500:
            issues.append("BATCH_SIZE must be between 1 and 500")
        if not 1 <= self.threads <= self.max_threads:
            issues.append(f"THREADS must be between 1 and MAX_THREADS ({self.max_threads})")
        if self.per_domain_delay < 0:
            issues.append("PER_DOMAIN_DELAY cannot be negative")
        if self.min_content_chars < 1:
            issues.append("MIN_CONTENT_CHARS must be at least 1")
        return issues

    def public(self) -> dict[str, Any]:
        """Everything except the secrets - safe to show in the panel."""
        hidden = {"api_key", "panel_password"}
        out = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in hidden}
        out["api_key_set"] = bool(self.api_key)
        out["config_path"] = getattr(self, "config_path", None)
        return out

    def apply(self, updates: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Apply live-tunable updates. Returns (applied, rejected_messages)."""
        applied: dict[str, Any] = {}
        rejected: list[str] = []
        by_name = {f.name: f for f in fields(self)}

        for key, value in updates.items():
            if key not in LIVE_TUNABLE:
                rejected.append(f"{key} is not changeable at runtime")
                continue
            try:
                coerced = _coerce(value, by_name[key].type)
            except (TypeError, ValueError):
                rejected.append(f"{key}: {value!r} is not a valid value")
                continue

            previous = getattr(self, key)
            setattr(self, key, coerced)
            problems = self.problems()
            if problems:
                setattr(self, key, previous)
                rejected.append(f"{key}: {problems[0]}")
                continue
            applied[key] = coerced

        return applied, rejected
