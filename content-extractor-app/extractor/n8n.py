"""Client for the n8n "Content Extractor API" workflow."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)


class N8nError(Exception):
    pass


class N8nAuthError(N8nError):
    """Wrong or missing X-API-Key - retrying will not help."""


class N8nClient:
    def __init__(self, settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None

        self.reachable: bool | None = None
        self.last_error: str | None = None
        self.last_claim_at: str | None = None
        self.last_save_at: str | None = None
        self.last_heartbeat_at: str | None = None

    # ------------------------------------------------------------------
    def _http(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                # Deliberately no proxy here: the scraping proxy must not sit
                # between this worker and its own control plane.
                self._client = httpx.Client(
                    headers={
                        "X-API-Key": self.settings.api_key,
                        "Content-Type": "application/json",
                        "User-Agent": f"content-extractor/{self.settings.node_name}",
                    },
                    timeout=httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0),
                    trust_env=False,
                )
            return self._client

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client:
            try:
                client.close()
            except Exception:
                pass

    def reset(self) -> None:
        """Drop the pooled client so changed settings take effect."""
        self.close()

    # ------------------------------------------------------------------
    def _post(self, url: str, payload: dict, *, attempts: int = 3,
              what: str = "request") -> dict:
        last: str = "unknown error"
        for attempt in range(1, attempts + 1):
            try:
                response = self._http().post(url, json=payload)

                # A key can be checked in two independent places, and they
                # answer differently - so say which one said no. Neither is
                # worth retrying.
                if response.status_code in (401, 403):
                    self.reachable = True
                    body = (response.text or "").strip()[:120]
                    if response.status_code == 403 or "Authorization data" in body:
                        culprit = "n8n's webhook Header Auth credential"
                    else:
                        culprit = "the workflow's Auth nodes"
                    self.last_error = (
                        f"{response.status_code} from {url.rsplit('/', 1)[-1]}: "
                        f"rejected by {culprit} - check N8N_API_KEY"
                        + (f" (reply: {body})" if body else "")
                    )
                    raise N8nAuthError(self.last_error)
                if response.status_code == 404:
                    self.reachable = True
                    self.last_error = (
                        f"404 from {url} - is the workflow Active? "
                        "(test-mode webhooks answer only one call)"
                    )
                    raise N8nError(self.last_error)
                if response.status_code >= 400:
                    last = f"HTTP {response.status_code}: {response.text[:160]}"
                else:
                    self.reachable = True
                    self.last_error = None
                    if not response.content:
                        return {}
                    try:
                        return response.json()
                    except ValueError:
                        last = f"non-JSON reply: {response.text[:160]}"

            except (N8nAuthError, N8nError):
                raise
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {str(exc)[:160]}"

            log.warning("%s failed (%d/%d): %s", what, attempt, attempts, last)
            if attempt < attempts:
                time.sleep(3 * attempt)

        self.reachable = False
        self.last_error = last
        raise N8nError(f"{what} failed after {attempts} attempts: {last}")

    # ------------------------------------------------------------------
    def claim(self, batch_size: int) -> list[dict]:
        """Claim a batch of URLs. Returns [] when the queue is empty."""
        data = self._post(
            self.settings.get_url,
            {"batch_size": int(batch_size), "node_name": self.settings.node_name},
            what="claim batch",
        )
        records = data.get("records") or []
        if not isinstance(records, list):
            raise N8nError(f"unexpected reply shape: {str(data)[:160]}")
        self.last_claim_at = datetime.now(timezone.utc).isoformat()
        return [r for r in records if isinstance(r, dict) and r.get("id") is not None]

    def save(self, results: list[dict]) -> dict:
        """Send extracted content back. Returns {'saved': n, 'failed': m}."""
        payload = {
            "node_name": self.settings.node_name,
            "worker_type": self.settings.worker_type,
            "results": [
                {
                    "id": r["id"],
                    "status": r["status"],
                    "content": r.get("content"),
                    "error": r.get("error"),
                    "http_status": r.get("http_status"),
                    "elapsed_ms": r.get("elapsed_ms"),
                }
                for r in results
            ],
        }
        data = self._post(self.settings.save_url, payload, what="save batch")
        self.last_save_at = datetime.now(timezone.utc).isoformat()
        return data

    def heartbeat(self, payload: dict) -> bool:
        """Best-effort telemetry - never raises, never blocks the worker."""
        try:
            self._post(self.settings.status_url, payload, attempts=1, what="heartbeat")
            self.last_heartbeat_at = datetime.now(timezone.utc).isoformat()
            return True
        except N8nAuthError:
            raise
        except N8nError:
            return False

    def snapshot(self) -> dict:
        return {
            "base_url": self.settings.base_url,
            "reachable": self.reachable,
            "last_error": self.last_error,
            "last_claim_at": self.last_claim_at,
            "last_save_at": self.last_save_at,
            "last_heartbeat_at": self.last_heartbeat_at,
        }
