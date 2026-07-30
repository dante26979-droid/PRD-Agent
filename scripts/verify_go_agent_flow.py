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


def mutate(path: str, payload: dict, idempotency_key: str) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = dict(HEADERS)
    headers["Content-Type"] = "application/json"
    headers["Idempotency-Key"] = idempotency_key
    call = urllib.request.Request(
        BASE_URL + path,
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(call, timeout=5) as response:
        return json.load(response)


def wait_for_run(task_id: str, run_id: str, timeout_seconds: float = 120) -> dict:
    deadline = time.monotonic() + timeout_seconds
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
    return final_run


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
    final_run = wait_for_run(task_id, run_id)

    _, task_body = request(f"/api/v1/tasks/{task_id}")
    task = json.loads(task_body)["task"]
    if task["version"] != 2:
        raise RuntimeError(f"draft was not committed: task version={task['version']}")

    _, draft_body = request(f"/api/v1/tasks/{task_id}/draft")
    working_draft = json.loads(draft_body)["content"]
    if working_draft.get("schema_version") != "working-draft.v2":
        raise RuntimeError(f"advanced draft schema is missing: {working_draft}")
    if working_draft.get("grounding_outcome") != "GROUNDED":
        raise RuntimeError(f"grounding loop did not pass: {working_draft}")
    if working_draft.get("quality_outcome") != "QUALITY_PASSED":
        raise RuntimeError(f"quality loop did not pass: {working_draft}")
    if not working_draft.get("confirmation_units"):
        raise RuntimeError(f"confirmation units were not built: {working_draft}")
    _, units_body = request(f"/api/v1/tasks/{task_id}/confirmation-units")
    units = json.loads(units_body)["items"]
    if len(units) != len(working_draft["confirmation_units"]):
        raise RuntimeError(f"confirmation unit persistence mismatch: {units}")
    original_hashes = {item["unit_key"]: item["content_hash"] for item in units}
    task_version = task["version"]
    for item in sorted(units, key=lambda value: value["order"]):
        confirmed = mutate(
            f"/api/v1/tasks/{task_id}/confirmation-units/{item['unit_version_id']}/confirm",
            {"expected_task_version": task_version},
            "local-confirm-" + uuid.uuid4().hex,
        )
        task_version = confirmed["task_version"]
    _, confirmed_task_body = request(f"/api/v1/tasks/{task_id}")
    confirmed_task = json.loads(confirmed_task_body)["task"]
    if confirmed_task["status"] != "REVIEWABLE":
        raise RuntimeError(f"task is not reviewable after confirmations: {confirmed_task}")

    reopened_unit = sorted(units, key=lambda value: value["order"])[-1]
    reopened = mutate(
        f"/api/v1/tasks/{task_id}/confirmation-units/{reopened_unit['unit_version_id']}/reopen",
        {
            "expected_task_version": task_version,
            "feedback": "补充本地部署异常恢复验收场景",
        },
        "local-reopen-" + uuid.uuid4().hex,
    )
    revision_run = reopened["run"]
    wait_for_run(task_id, revision_run["run_id"])
    _, revised_units_body = request(f"/api/v1/tasks/{task_id}/confirmation-units")
    revised_units = json.loads(revised_units_body)["items"]
    for item in revised_units:
        if item["unit_key"] == reopened_unit["unit_key"]:
            if item["content_hash"] == original_hashes[item["unit_key"]]:
                raise RuntimeError("reopened unit content did not change")
            if item["confirmation_status"] != "PENDING":
                raise RuntimeError(f"reopened unit should require confirmation: {item}")
        else:
            if item["content_hash"] != original_hashes[item["unit_key"]]:
                raise RuntimeError(f"immutable unit changed during scoped revision: {item}")
            if item["confirmation_status"] != "CONFIRMED":
                raise RuntimeError(f"immutable confirmation was not carried: {item}")

    _, events_body = request(f"/api/v1/tasks/{task_id}/events")
    events = events_body.decode("utf-8")
    expected_events = (
        "agent.MODEL_ATTEMPT",
        "agent.RUN_ARTIFACT_SAVED",
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
                "draft_schema_version": working_draft["schema_version"],
                "confirmation_unit_count": len(
                    working_draft["confirmation_units"]
                ),
                "confirmed_task_version": task_version,
                "revision_run_id": revision_run["run_id"],
                "reopened_unit_key": reopened_unit["unit_key"],
                "verified_events": expected_events,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
