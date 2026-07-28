from __future__ import annotations

import argparse
from dataclasses import asdict
from enum import Enum
import json
import os
from typing import Any

from prd_agent.domain.commands import (
    ConfirmOutline,
    ConfirmUnit,
    FinalizePrd,
    ReopenPrd,
    ReplyToTask,
    StartTask,
)
from prd_agent.historical.ingest import HistoricalPrdIngestor
from prd_agent.historical.manifest import load_manifest
from prd_agent.quality.service import DocumentQualityService
from prd_agent.storage.postgres_historical import PostgresHistoricalPrdStore
from prd_agent.storage.postgres import PostgresWorkflowRepository
from prd_agent.workflow.service import WorkflowService
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def snapshot_payload(snapshot) -> dict[str, Any]:
    outline = snapshot.current_outline
    return _jsonable(
        {
            "task": asdict(snapshot.task),
            "run": asdict(snapshot.run),
            "requirement_brief": (
                snapshot.current_brief.brief.as_dict() if snapshot.current_brief else None
            ),
            "outline": asdict(outline) if outline else None,
            "markdown": snapshot.markdown,
        }
    )


def _service(args: argparse.Namespace) -> WorkflowService:
    dsn = args.dsn or os.environ.get("PRD_AGENT_DATABASE_DSN")
    if not dsn:
        raise SystemExit("--dsn or PRD_AGENT_DATABASE_DSN is required")
    return WorkflowService(
        PostgresWorkflowRepository.from_dsn(dsn),
        HeuristicWorkflowModel(),
        quality_service=DocumentQualityService(),
    )


def _run(args: argparse.Namespace) -> int:
    service = _service(args)
    if args.workflow_command == "start":
        result = service.start_task(StartTask(args.message, args.idempotency_key))
    elif args.workflow_command == "show":
        result = service.show(args.task_id)
    elif args.workflow_command == "reply":
        result = service.reply_to_task(
            ReplyToTask(
                args.task_id,
                args.message,
                args.expected_task_version,
                args.idempotency_key,
            )
        )
    elif args.workflow_command == "confirm-outline":
        result = service.confirm_outline(
            ConfirmOutline(
                args.task_id,
                args.outline_version,
                args.expected_task_version,
                args.idempotency_key,
            )
        )
    elif args.workflow_command == "confirm-unit":
        result = service.confirm_unit(
            ConfirmUnit(
                args.task_id,
                args.unit_id,
                args.expected_task_version,
                args.idempotency_key,
            )
        )
    elif args.workflow_command == "finalize":
        result = service.finalize_prd(
            FinalizePrd(
                args.task_id,
                args.document_id,
                args.content_hash,
                args.expected_task_version,
                args.idempotency_key,
            )
        )
    else:
        result = service.reopen_prd(
            ReopenPrd(
                args.task_id,
                tuple(args.unit_id),
                args.reason,
                args.expected_task_version,
                args.idempotency_key,
            )
        )
    print(json.dumps(snapshot_payload(result), ensure_ascii=False, indent=2))
    return 0


def _historical_prd_run(args: argparse.Namespace) -> int:
    if args.historical_prd_command == "validate":
        manifest, document_root = load_manifest(
            args.manifest,
            allowed_root=args.allowed_root,
        )
        payload = {
            "status": "VALID",
            "corpus_id": manifest.corpus_id,
            "document_count": len(manifest.documents),
            "manifest_hash": manifest.manifest_hash,
            "document_root": str(document_root),
        }
    elif args.historical_prd_command == "ingest":
        dsn = args.dsn or os.environ.get("PRD_AGENT_DATABASE_DSN")
        if not dsn:
            raise SystemExit("--dsn or PRD_AGENT_DATABASE_DSN is required")
        store = PostgresHistoricalPrdStore.from_dsn(dsn)
        try:
            corpus = HistoricalPrdIngestor(store).ingest_manifest(
                args.manifest,
                allowed_root=args.allowed_root,
            )
            payload = corpus.model_dump(mode="json")
        finally:
            store.connection.close()
    else:
        dsn = args.dsn or os.environ.get("PRD_AGENT_DATABASE_DSN")
        if not dsn:
            raise SystemExit("--dsn or PRD_AGENT_DATABASE_DSN is required")
        store = PostgresHistoricalPrdStore.from_dsn(dsn)
        try:
            with store.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status FROM historical_corpora
                     WHERE corpus_id = %s AND corpus_version = %s
                    """,
                    (args.corpus_id, args.corpus_version),
                )
                row = cursor.fetchone()
                if not row or row[0] != "READY":
                    raise SystemExit("READY corpus version was not found")
                cursor.execute("ANALYZE historical_prd_chunks")
            store.connection.commit()
            payload = {
                "status": "READY",
                "corpus_id": args.corpus_id,
                "corpus_version": args.corpus_version,
                "index": "KEYWORD",
            }
        finally:
            store.connection.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prd-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    workflow = commands.add_parser("workflow")
    workflow.add_argument("--dsn")
    actions = workflow.add_subparsers(dest="workflow_command", required=True)

    start = actions.add_parser("start")
    start.add_argument("--message", required=True)
    start.add_argument("--idempotency-key", default="cli-start")

    show = actions.add_parser("show")
    show.add_argument("--task-id", required=True)

    reply = actions.add_parser("reply")
    reply.add_argument("--task-id", required=True)
    reply.add_argument("--message", required=True)
    reply.add_argument("--expected-task-version", type=int, required=True)
    reply.add_argument("--idempotency-key", default="cli-reply")

    confirm_outline = actions.add_parser("confirm-outline")
    confirm_outline.add_argument("--task-id", required=True)
    confirm_outline.add_argument("--outline-version", type=int, required=True)
    confirm_outline.add_argument("--expected-task-version", type=int, required=True)
    confirm_outline.add_argument("--idempotency-key", default="cli-confirm-outline")

    confirm_unit = actions.add_parser("confirm-unit")
    confirm_unit.add_argument("--task-id", required=True)
    confirm_unit.add_argument("--unit-id", required=True)
    confirm_unit.add_argument("--expected-task-version", type=int, required=True)
    confirm_unit.add_argument("--idempotency-key", default="cli-confirm-unit")

    finalize = actions.add_parser("finalize")
    finalize.add_argument("--task-id", required=True)
    finalize.add_argument("--document-id", required=True)
    finalize.add_argument("--content-hash", required=True)
    finalize.add_argument("--expected-task-version", type=int, required=True)
    finalize.add_argument("--idempotency-key", default="cli-finalize")

    reopen = actions.add_parser("reopen")
    reopen.add_argument("--task-id", required=True)
    reopen.add_argument("--unit-id", action="append", required=True)
    reopen.add_argument("--reason", required=True)
    reopen.add_argument("--expected-task-version", type=int, required=True)
    reopen.add_argument("--idempotency-key", default="cli-reopen")
    workflow.set_defaults(handler=_run)

    historical = commands.add_parser("historical-prd")
    historical_actions = historical.add_subparsers(
        dest="historical_prd_command", required=True
    )
    validate = historical_actions.add_parser("validate")
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--allowed-root", required=True)

    ingest = historical_actions.add_parser("ingest")
    ingest.add_argument("--manifest", required=True)
    ingest.add_argument("--allowed-root", required=True)
    ingest.add_argument("--dsn")

    build_index = historical_actions.add_parser("build-index")
    build_index.add_argument("--corpus-id", required=True)
    build_index.add_argument("--corpus-version", required=True)
    build_index.add_argument("--dsn")
    historical.set_defaults(handler=_historical_prd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)
