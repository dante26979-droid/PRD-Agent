from __future__ import annotations

import sys

import pytest

from agent import worker_main


def test_rpc_server_reserves_control_threads_for_cancel_and_health():
    assert worker_main._rpc_worker_count(max_inflight=1, configured=None) >= 3
    assert worker_main._rpc_worker_count(max_inflight=4, configured=None) >= 6


def test_rpc_server_rejects_configured_pool_without_control_capacity():
    with pytest.raises(ValueError, match="max_workers"):
        worker_main._rpc_worker_count(max_inflight=2, configured=2)


def test_worker_main_uses_default_agent_loop_factory(monkeypatch):
    captured = {}
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("PRD_AGENT_AGENT_RPC_TOKEN", "t" * 32)
    monkeypatch.delenv("PRD_AGENT_AGENT_LOOP_FACTORY", raising=False)
    monkeypatch.setattr(sys, "argv", ["prd-agent-agent-worker", "--endpoint", ":0"])

    def fake_serve(loop, **kwargs):
        captured["loop"] = loop
        captured.update(kwargs)

    monkeypatch.setattr(worker_main, "serve", fake_serve)

    worker_main.main()

    assert callable(captured["loop"])
    assert captured["endpoint"] == "0.0.0.0:0"
    assert captured["worker_id"] == "worker-1"
