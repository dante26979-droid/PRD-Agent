from __future__ import annotations

import argparse
from concurrent import futures
import importlib
import os

import grpc

from agent.v1 import agent_worker_pb2_grpc as worker_rpc

from .config import AgentSettings
from .worker_server import AgentWorkerServer


def serve(
    loop,
    *,
    endpoint: str,
    worker_id: str,
    max_inflight: int = 1,
    max_workers: int | None = None,
    max_checkpoint_bytes: int = 256 * 1024,
    max_draft_bytes: int = 1024 * 1024,
    max_run_artifact_bytes: int = 1024 * 1024,
    max_evidence_items: int = 100,
    model_ready: bool = True,
    capability_ready: bool = False,
    service_token: str | None = None,
    event_ack_timeout_seconds: float = 30,
) -> None:
    server = grpc.server(
        futures.ThreadPoolExecutor(
            max_workers=_rpc_worker_count(
                max_inflight=max_inflight,
                configured=max_workers,
            )
        )
    )
    worker_rpc.add_AgentWorkerServiceServicer_to_server(
        AgentWorkerServer(
            loop,
            worker_id=worker_id,
            max_inflight=max_inflight,
            max_checkpoint_bytes=max_checkpoint_bytes,
            max_draft_bytes=max_draft_bytes,
            max_run_artifact_bytes=max_run_artifact_bytes,
            max_evidence_items=max_evidence_items,
            model_ready=model_ready,
            capability_ready=capability_ready,
            service_token=service_token,
            event_ack_timeout_seconds=event_ack_timeout_seconds,
        ),
        server,
    )
    if server.add_insecure_port(endpoint) == 0:
        raise RuntimeError(f"failed to bind Agent Worker endpoint: {endpoint}")
    server.start()
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(5).wait()
    finally:
        server.stop(5)


def load_loop(spec: str):
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("agent loop factory must use module:attribute syntax")
    factory = getattr(importlib.import_module(module_name), attribute)
    return factory() if callable(factory) else factory


def _rpc_worker_count(*, max_inflight: int, configured: int | None) -> int:
    minimum = max_inflight + 2
    if configured is None:
        return minimum
    if configured < minimum:
        raise ValueError(
            "max_workers must reserve at least two threads beyond max_inflight "
            "for cancellation and health RPCs"
        )
    return configured


def _bind_endpoint(endpoint: str) -> str:
    if endpoint.startswith(":"):
        return "0.0.0.0" + endpoint
    return endpoint


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=os.getenv("PRD_AGENT_AGENT_WORKER_ENDPOINT", ":9100"))
    parser.add_argument("--worker-id", default=os.getenv("PRD_AGENT_AGENT_WORKER_ID", "worker-1"))
    parser.add_argument("--max-inflight", type=int, default=int(os.getenv("PRD_AGENT_AGENT_WORKER_MAX_INFLIGHT", "1")))
    parser.add_argument(
        "--loop-factory",
        default=os.getenv(
            "PRD_AGENT_AGENT_LOOP_FACTORY",
            "agent.bootstrap:build_agent_loop",
        ),
    )
    args = parser.parse_args()
    settings = AgentSettings.load(require_service_identity=True)
    serve(
        load_loop(args.loop_factory),
        endpoint=_bind_endpoint(args.endpoint),
        worker_id=args.worker_id,
        max_inflight=args.max_inflight,
        max_checkpoint_bytes=settings.max_checkpoint_bytes,
        max_draft_bytes=settings.max_draft_bytes,
        max_run_artifact_bytes=settings.max_run_artifact_bytes,
        max_evidence_items=settings.max_evidence_items,
        model_ready=True,
        capability_ready=settings.capability_target is not None,
        service_token=settings.service_token,
        event_ack_timeout_seconds=settings.event_ack_timeout_seconds,
    )


if __name__ == "__main__":
    main()
