#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid


BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:18080"
HEADERS = {
    "X-Tenant-ID": "local-e2e",
    "X-User-ID": "local-e2e-user",
}


def request(path: str, *, method: str = "GET", payload: dict | None = None) -> tuple[int, bytes]:
    body = None
    headers = dict(HEADERS)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(
        BASE_URL + path,
        data=body,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(call, timeout=5) as response:
        return response.status, response.read()


def wait_until_ready(timeout_seconds: float = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            status, body = request("/api/v1/health/ready")
            if status == 200 and json.loads(body)["status"] == "ready":
                return
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(1)
    raise RuntimeError("Go API did not become ready")


def main() -> None:
    wait_until_ready()
    payload = {"message": "为代码审查助手生成一份可评审的 PRD"}
    headers = dict(HEADERS)
    headers["Content-Type"] = "application/json"
    headers["Idempotency-Key"] = "local-e2e-" + uuid.uuid4().hex
    create = urllib.request.Request(
        BASE_URL + "/api/v1/tasks/from-message",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(create, timeout=5) as response:
        if response.status != 201:
            raise RuntimeError(f"task creation returned {response.status}")
        created = json.load(response)

    task_id = created["task"]["task_id"]
    run_id = created["run"]["run_id"]
    deadline = time.monotonic() + 120
    final_run = None
    while time.monotonic() < deadline:
        _, body = request(f"/api/v1/tasks/{task_id}/runs")
        runs = json.loads(body)["items"]
        final_run = next(item for item in runs if item["run_id"] == run_id)
        if final_run["status"] in {"SUCCEEDED", "FAILED", "STOPPED"}:
            break
        time.sleep(1)
    if final_run is None or final_run["status"] != "SUCCEEDED":
        raise RuntimeError(f"Agent Run did not succeed: {final_run}")

    _, task_body = request(f"/api/v1/tasks/{task_id}")
    task = json.loads(task_body)["task"]
    if task["version"] != 2:
        raise RuntimeError(f"draft was not committed: task version={task['version']}")

    _, events_body = request(f"/api/v1/tasks/{task_id}/events")
    events = events_body.decode("utf-8")
    expected_events = (
        "agent.MODEL_ATTEMPT",
        "agent.CHECKPOINT_SAVED",
        "agent.DRAFT_SUBMITTED",
        "agent.RUN_COMPLETED",
    )
    missing = [event for event in expected_events if f"event: {event}" not in events]
    if missing:
        raise RuntimeError(f"missing projected events: {missing}")

    print(
        json.dumps(
            {
                "status": "ok",
                "task_id": task_id,
                "run_id": run_id,
                "run_status": final_run["status"],
                "task_version": task["version"],
                "verified_events": expected_events,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
