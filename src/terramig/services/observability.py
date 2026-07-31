from __future__ import annotations

import json
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any

from ..persistence import Persistence


class Observability:
    """In-process operational telemetry with Prometheus and JSON views."""

    def __init__(self, recent_limit: int = 100) -> None:
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.requests: Counter[tuple[str, str, int]] = Counter()
        self.operations: Counter[tuple[str, str]] = Counter()
        self.recent: deque[dict[str, Any]] = deque(maxlen=recent_limit)
        self.persistence: Persistence | None = None

    def attach(self, persistence: Persistence) -> None:
        self.persistence = persistence

    def record_request(
        self, method: str, path: str, status: int, duration_ms: int, request_id: str
    ) -> None:
        route = self._route(path)
        event = {
            "timestamp": self._now(),
            "kind": "http",
            "name": f"{method} {route}",
            "status": str(status),
            "duration_ms": duration_ms,
            "request_id": request_id,
        }
        with self.lock:
            self.requests[(method, route, status)] += 1
            self.recent.append(event)
        self._persist(event)

    def record_operation(
        self,
        name: str,
        status: str,
        duration_ms: int,
        *,
        subject_id: str = "",
    ) -> None:
        event = {
            "timestamp": self._now(),
            "kind": "operation",
            "name": name,
            "status": status,
            "duration_ms": duration_ms,
            "subject_id": subject_id,
        }
        with self.lock:
            self.operations[(name, status)] += 1
            self.recent.append(event)
        self._persist(event)

    def snapshot(self, workflows: list[Any]) -> dict[str, Any]:
        workflow_stages = Counter(item.stage.value for item in workflows)
        persisted = (
            self.persistence.list_audit_events(500) if self.persistence else []
        )
        with self.lock:
            recent = persisted[:100] if persisted else list(reversed(self.recent))
            operations = (
                Counter(
                    (item["name"], item["status"])
                    for item in persisted
                    if item.get("kind") == "operation"
                )
                if persisted
                else self.operations
            )
            return {
                "uptime_seconds": round(time.monotonic() - self.started),
                "workflows": {
                    "total": len(workflows),
                    "by_stage": dict(sorted(workflow_stages.items())),
                },
                "operations": [
                    {"name": name, "status": status, "count": count}
                    for (name, status), count in sorted(operations.items())
                ],
                "recent": recent,
            }

    def prometheus(self, workflows: list[Any]) -> str:
        lines = [
            "# HELP terramig_uptime_seconds TerraMig process uptime.",
            "# TYPE terramig_uptime_seconds gauge",
            f"terramig_uptime_seconds {time.monotonic() - self.started:.3f}",
            "# HELP terramig_workflows Current workflows by stage.",
            "# TYPE terramig_workflows gauge",
        ]
        for stage, count in sorted(Counter(item.stage.value for item in workflows).items()):
            lines.append(f'terramig_workflows{{stage="{stage}"}} {count}')
        with self.lock:
            lines.extend(
                [
                    "# HELP terramig_http_requests_total HTTP responses.",
                    "# TYPE terramig_http_requests_total counter",
                ]
            )
            for (method, route, status), count in sorted(self.requests.items()):
                lines.append(
                    "terramig_http_requests_total"
                    f'{{method="{self._label(method)}",'
                    f'route="{self._label(route)}",status="{status}"}} {count}'
                )
            lines.extend(
                [
                    "# HELP terramig_operations_total Workflow operations.",
                    "# TYPE terramig_operations_total counter",
                ]
            )
            for (name, status), count in sorted(self.operations.items()):
                lines.append(
                    "terramig_operations_total"
                    f'{{operation="{self._label(name)}",'
                    f'status="{self._label(status)}"}} {count}'
                )
        return "\n".join(lines) + "\n"

    @staticmethod
    def structured_log(event: dict[str, Any]) -> str:
        return json.dumps(event, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _route(path: str) -> str:
        clean = path.split("?", 1)[0]
        segments = clean.strip("/").split("/")
        normalized = [
            "{id}" if len(segment) >= 24 and "-" in segment else segment
            for segment in segments
        ]
        return "/" + "/".join(normalized)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _label(value: str) -> str:
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    def _persist(self, event: dict[str, Any]) -> None:
        if not self.persistence:
            return
        try:
            self.persistence.record_audit_event(event)
        except Exception as error:
            print(
                self.structured_log(
                    {
                        "event": "audit_persistence_failed",
                        "error_type": type(error).__name__,
                    }
                )
            )
