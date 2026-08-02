from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from prd_agent.eval.cli import _load_config, run_baseline_command


ROOT = Path(__file__).resolve().parents[2]


def test_load_config_preserves_langgraph_runtime_options():
    config = _load_config(ROOT / "eval/configs/langgraph_v1_characterization.json")

    assert config.config_id == "langgraph-runtime-v1-characterization"
    assert config.options["workflow_version"] == "agent-runtime.v1"
    assert config.options["fixture_path"] == "eval/fixtures/langgraph_v1_actions.json"
    assert config.options["max_iterations"] == 5


def test_report_filename_rejects_path_traversal(tmp_path, monkeypatch):
    config_file = tmp_path / "unsafe.json"
    config_file.write_text(
        json.dumps(
            {
                "config_id": "../escape",
                "prompt_version": "v1",
                "model_id": "stub",
                "dataset_version": "eval-v1",
                "trials_per_case": 1,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(ROOT)
    args = argparse.Namespace(
        manifest=str(ROOT / "eval/cases/manifest.json"),
        config=str(config_file),
        dsn=None,
        output_dir=str(tmp_path / "reports"),
    )

    with pytest.raises(ValueError, match="safe report filename"):
        run_baseline_command(args)

    assert not (tmp_path / "escape.json").exists()
