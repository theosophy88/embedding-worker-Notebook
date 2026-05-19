import json
import os
import platform
import socket
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional

import psutil
import requests

try:
    import torch
except ImportError:
    torch = None

DEFAULT_N8N_STATUS_URL = "https://n8n.3rfan.ir/webhook/4041b926-a735-45fc-a002-1f3ecfb11691"
DEFAULT_STATUS_INTERVAL = 100


class WorkerStatusReporter:
    """Post heartbeat JSON to an n8n webhook using X-API-Key authentication."""

    def __init__(
        self,
        node_name: str,
        model_name: str,
        batch_size: int,
        n8n_status_url: str = DEFAULT_N8N_STATUS_URL,
        n8n_api_key: Optional[str] = None,
        status_interval: int = DEFAULT_STATUS_INTERVAL,
    ):
        self.node_name = node_name
        self.model_name = model_name
        self.batch_size = batch_size
        self.n8n_status_url = n8n_status_url
        self.n8n_api_key = n8n_api_key
        self.status_interval = status_interval
        self.session_started_at = datetime.now(timezone.utc)
        self.start_time = time.time()

        self.cycles = 0
        self.articles_fetched = 0
        self.articles_embedded = 0
        self.articles_errors = 0
        self.delay_seconds = 0.0
        self.embedding_timestamps = deque()

    def disable(self):
        self.n8n_status_url = None

    def set_delay(self, seconds: float):
        self.delay_seconds = float(seconds)

    def record_cycle(self, fetched: int = 0):
        self.cycles += 1
        self.articles_fetched += int(fetched)

    def record_embeddings(self, count: int = 1):
        self.articles_embedded += int(count)
        now = time.time()
        for _ in range(int(count)):
            self.embedding_timestamps.append(now)

    def record_errors(self, count: int = 1):
        self.articles_errors += int(count)

    def _get_server_lan_ip(self) -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.5)
                sock.connect(("8.8.8.8", 80))
                return sock.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    def _get_system_metrics(self) -> dict:
        vm = psutil.virtual_memory()
        cpu_percent = psutil.cpu_percent(interval=None)

        try:
            load1, load5, load15 = os.getloadavg()
        except Exception:
            load1 = load5 = load15 = 0.0

        try:
            affinity = psutil.Process().cpu_affinity()
            cores_allowed = len(affinity)
            cores_active = cores_allowed
        except Exception:
            cores_allowed = psutil.cpu_count(logical=True) or 0
            cores_active = cores_allowed

        return {
            "server_host": socket.gethostname(),
            "server_lan_ip": self._get_server_lan_ip(),
            "server_os": platform.system(),
            "server_platform": platform.platform(),
            "cores_logical": psutil.cpu_count(logical=True) or 0,
            "cores_physical": psutil.cpu_count(logical=False) or 0,
            "cores_allowed": cores_allowed,
            "cores_active": cores_active,
            "cpu_percent": round(cpu_percent, 2),
            "load_average_1m": round(load1, 2),
            "load_average_5m": round(load5, 2),
            "load_average_15m": round(load15, 2),
            "memory_total_bytes": vm.total,
            "memory_available_bytes": vm.available,
            "memory_used_percent": round(vm.percent, 2),
        }

    def _get_embedding_history(self) -> tuple[int, float]:
        cutoff = time.time() - 3600
        while self.embedding_timestamps and self.embedding_timestamps[0] < cutoff:
            self.embedding_timestamps.popleft()
        count_last_hour = len(self.embedding_timestamps)
        return count_last_hour, round(count_last_hour / 60, 2)

    def _make_payload(self, status: str, stop_time: Optional[datetime] = None) -> dict:
        now = datetime.now(timezone.utc)
        uptime = max(time.time() - self.start_time, 0.0)
        avg_per_minute = round(self.articles_embedded / (uptime / 60), 2) if uptime > 0 else 0.0
        avg_per_hour = round(self.articles_embedded / (uptime / 3600), 2) if uptime > 0 else 0.0
        embeddings_last_hour, embeddings_last_hour_per_minute = self._get_embedding_history()

        remainder = self.status_interval - (self.articles_embedded % self.status_interval)
        if remainder == self.status_interval:
            remainder = self.status_interval if self.articles_embedded > 0 else 0

        next_seconds = round(remainder / (self.articles_embedded / uptime), 2) if self.articles_embedded > 0 and uptime > 0 else 0.0
        next_status_at = (now + timedelta(seconds=next_seconds)).isoformat()

        metrics = self._get_system_metrics()

        return {
            "node_name": self.node_name,
            "status": status,
            "cycles": self.cycles,
            "batch_size": self.batch_size,
            "delay_seconds": round(self.delay_seconds, 2),
            "articles_fetched": self.articles_fetched,
            "articles_embedded": self.articles_embedded,
            "articles_errors": self.articles_errors,
            "device": "cuda" if torch is not None and getattr(torch, "cuda", None) is not None and torch.cuda.is_available() else "cpu",
            "model_name": self.model_name,
            "server_host": metrics["server_host"],
            "server_lan_ip": metrics["server_lan_ip"],
            "server_os": metrics["server_os"],
            "server_platform": metrics["server_platform"],
            "session_started_at": self.session_started_at.isoformat(),
            "session_uptime_seconds": round(uptime, 2),
            "stop_time": stop_time.isoformat() if stop_time else None,
            "status_interval": self.status_interval,
            "next_status_in_seconds": next_seconds,
            "next_status_at": next_status_at,
            "avg_embeddings_per_hour": avg_per_hour,
            "avg_embeddings_per_minute": avg_per_minute,
            "embeddings_last_hour": embeddings_last_hour,
            "embeddings_last_hour_per_minute": embeddings_last_hour_per_minute,
            "cores_logical": metrics["cores_logical"],
            "cores_physical": metrics["cores_physical"],
            "cores_allowed": metrics["cores_allowed"],
            "cores_active": metrics["cores_active"],
            "cpu_percent": metrics["cpu_percent"],
            "load_average_1m": metrics["load_average_1m"],
            "load_average_5m": metrics["load_average_5m"],
            "load_average_15m": metrics["load_average_15m"],
            "memory_total_bytes": metrics["memory_total_bytes"],
            "memory_available_bytes": metrics["memory_available_bytes"],
            "memory_used_percent": metrics["memory_used_percent"],
        }

    def send_heartbeat(self, status: str = "running", stop_time: Optional[datetime] = None) -> bool:
        if not self.n8n_status_url:
            return False

        headers = {"Content-Type": "application/json"}
        if self.n8n_api_key:
            headers["X-API-Key"] = self.n8n_api_key

        payload = self._make_payload(status=status, stop_time=stop_time)
        response = requests.post(self.n8n_status_url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        return response.status_code in (200, 201)


if __name__ == "__main__":
    import torch

    reporter = WorkerStatusReporter(
        node_name="worker-cpu-1",
        model_name="Qwen/Qwen3-Embedding-8B",
        batch_size=10,
        n8n_api_key="mer30kehasti",
    )
    reporter.record_cycle(fetched=10)
    reporter.record_embeddings(count=10)
    reporter.record_errors(count=0)
    reporter.set_delay(5)

    try:
        success = reporter.send_heartbeat(status="running")
        print("Heartbeat posted:" if success else "Heartbeat disabled or failed")
        print(json.dumps(reporter._make_payload("running"), indent=2))
    except Exception as exc:
        print("Heartbeat error:", exc)
