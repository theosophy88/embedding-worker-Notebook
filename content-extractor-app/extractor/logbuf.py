"""Logging: stdout (journald picks it up) plus an in-memory ring for the panel."""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone

_RING_SIZE = 1000


class RingHandler(logging.Handler):
    """Keeps the last N formatted records so the panel can show a log tail."""

    def __init__(self, capacity: int = _RING_SIZE) -> None:
        super().__init__()
        self._lines: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # never let logging break the worker
            return
        entry = {
            "at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        with self._lock:
            self._lines.append(entry)

    def tail(self, count: int = 200) -> list[dict]:
        with self._lock:
            lines = list(self._lines)
        return lines[-count:]


RING = RingHandler()


def setup_logging(level: str = "INFO") -> None:
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    plain = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(plain)
    root.addHandler(stream)

    RING.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(RING)

    # These are chatty and say nothing we do not already log ourselves:
    # every extraction failure is reported through our own result records.
    for noisy in ("httpx", "httpcore", "urllib3", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for very_noisy in ("trafilatura", "trafilatura.core", "trafilatura.metadata"):
        logging.getLogger(very_noisy).setLevel(logging.ERROR)
