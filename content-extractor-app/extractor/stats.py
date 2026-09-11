"""Live counters for the panel and the n8n heartbeat."""
from __future__ import annotations

import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone

MINUTES_KEPT = 60
RECENT_KEPT = 60


class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = datetime.now(timezone.utc)
        self._start_monotonic = time.monotonic()

        self.state = "starting"        # starting|running|idle|paused|stopping|stopped|error
        self.state_detail: str | None = None

        self.cycles = 0
        self.claimed = 0
        self.extracted = 0
        self.failed = 0
        self.saved_batches = 0
        self.save_failures = 0
        self.empty_cycles = 0
        self.rendered = 0

        self.batch_total = 0
        self.batch_done = 0
        self.in_flight = 0
        self.last_cycle_seconds = 0.0

        self._errors: Counter[str] = Counter()
        self._recent: deque[dict] = deque(maxlen=RECENT_KEPT)
        self._minutes: dict[str, dict[str, int]] = {}
        self._chars_total = 0

    # ------------------------------------------------------------------
    @staticmethod
    def _minute_key(when: datetime) -> str:
        return when.strftime("%Y-%m-%dT%H:%M")

    def _trim_minutes(self) -> None:
        if len(self._minutes) <= MINUTES_KEPT * 2:
            return
        for key in sorted(self._minutes)[:-MINUTES_KEPT]:
            self._minutes.pop(key, None)

    # ------------------------------------------------------------------
    def set_state(self, state: str, detail: str | None = None) -> None:
        with self._lock:
            self.state = state
            self.state_detail = detail

    def start_batch(self, size: int) -> None:
        with self._lock:
            self.cycles += 1
            self.claimed += size
            self.batch_total = size
            self.batch_done = 0

    def task_started(self) -> None:
        with self._lock:
            self.in_flight += 1

    def record_result(self, result: dict) -> None:
        now = datetime.now(timezone.utc)
        ok = result.get("status") == "content"
        with self._lock:
            self.in_flight = max(self.in_flight - 1, 0)
            self.batch_done += 1
            if ok:
                self.extracted += 1
                self._chars_total += int(result.get("chars") or 0)
                if result.get("via") == "render":
                    self.rendered += 1
            else:
                self.failed += 1
                reason = (result.get("error") or "unknown").split(":")[0]
                self._errors[reason] += 1

            bucket = self._minutes.setdefault(self._minute_key(now), {"ok": 0, "fail": 0})
            bucket["ok" if ok else "fail"] += 1
            self._trim_minutes()

            self._recent.appendleft({
                "at": now.isoformat(timespec="seconds"),
                "id": result.get("id"),
                "url": result.get("url"),
                "host": result.get("host"),
                "status": result.get("status"),
                "chars": result.get("chars"),
                "http_status": result.get("http_status"),
                "elapsed_ms": result.get("elapsed_ms"),
                "via": result.get("via"),
                "method": result.get("method"),
                "error": result.get("error"),
            })

    def record_cycle(self, seconds: float, empty: bool) -> None:
        with self._lock:
            self.last_cycle_seconds = round(seconds, 2)
            self.empty_cycles = self.empty_cycles + 1 if empty else 0

    def record_save(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self.saved_batches += 1
            else:
                self.save_failures += 1

    # ------------------------------------------------------------------
    def uptime_seconds(self) -> float:
        return max(time.monotonic() - self._start_monotonic, 0.0)

    def throughput(self) -> list[dict]:
        """Per-minute ok/fail for the last hour, oldest first, gaps filled."""
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        series = []
        with self._lock:
            for step in range(MINUTES_KEPT - 1, -1, -1):
                minute = now.timestamp() - step * 60
                when = datetime.fromtimestamp(minute, timezone.utc)
                bucket = self._minutes.get(self._minute_key(when), {"ok": 0, "fail": 0})
                series.append({
                    "minute": when.strftime("%H:%M"),
                    "ok": bucket["ok"],
                    "fail": bucket["fail"],
                })
        return series

    def snapshot(self, settings=None) -> dict:
        uptime = self.uptime_seconds()
        with self._lock:
            processed = self.extracted + self.failed
            last_hour = sum(b["ok"] for b in self._minutes.values())
            data = {
                "state": self.state,
                "state_detail": self.state_detail,
                "started_at": self.started_at.isoformat(),
                "uptime_seconds": round(uptime, 1),
                "totals": {
                    "cycles": self.cycles,
                    "claimed": self.claimed,
                    "extracted": self.extracted,
                    "failed": self.failed,
                    "processed": processed,
                    "saved_batches": self.saved_batches,
                    "save_failures": self.save_failures,
                    "rendered": self.rendered,
                    "empty_cycles": self.empty_cycles,
                    "success_rate": round(100 * self.extracted / processed, 1) if processed else 0.0,
                    "avg_chars": round(self._chars_total / self.extracted) if self.extracted else 0,
                },
                "rates": {
                    "per_minute": round(processed / (uptime / 60), 1) if uptime > 1 else 0.0,
                    "extracted_per_hour": round(self.extracted / (uptime / 3600), 0) if uptime > 60 else 0.0,
                    "extracted_last_hour": last_hour,
                },
                "current": {
                    "batch_total": self.batch_total,
                    "batch_done": self.batch_done,
                    "in_flight": self.in_flight,
                    "last_cycle_seconds": self.last_cycle_seconds,
                },
                "errors": [
                    {"reason": reason, "count": count}
                    for reason, count in self._errors.most_common(10)
                ],
                "recent": list(self._recent),
            }
        data["throughput"] = self.throughput()
        return data

    def heartbeat_payload(self, settings) -> dict:
        snap = self.snapshot()
        totals, rates = snap["totals"], snap["rates"]
        return {
            "node_name": settings.node_name,
            "worker_type": settings.worker_type,
            "status": snap["state"],
            "cycles": totals["cycles"],
            "batch_size": settings.batch_size,
            "threads": settings.threads,
            "urls_fetched": totals["claimed"],
            "urls_extracted": totals["extracted"],
            "urls_failed": totals["failed"],
            "urls_rendered": totals["rendered"],
            "success_rate": totals["success_rate"],
            "avg_chars": totals["avg_chars"],
            "avg_per_minute": rates["per_minute"],
            "avg_per_hour": rates["extracted_per_hour"],
            "extractions_last_hour": rates["extracted_last_hour"],
            "session_started_at": snap["started_at"],
            "session_uptime_seconds": snap["uptime_seconds"],
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "top_errors": {e["reason"]: e["count"] for e in snap["errors"][:6]},
            "render_fallback": settings.render_fallback,
            "app": "content-extractor-app",
        }
