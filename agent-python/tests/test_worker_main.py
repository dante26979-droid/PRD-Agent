from __future__ import annotations

import sys

from agent import worker_main


def test_worker_main_uses_default_agent_loop_factory(monkeypatch):
    captured = {}
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
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
