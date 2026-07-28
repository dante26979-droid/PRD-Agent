import json

from prd_agent.cli import build_parser, main


def test_historical_validate_command_reports_manifest_without_database(
    tmp_path, capsys
):
    (tmp_path / "doc.md").write_text("# Demo\n\n历史规则", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """
corpus_id: demo
owner_id: owner
project_id: project
documents:
  - document_id: doc
    path: doc.md
    source_uri: demo://doc
    source_revision: "1"
""".strip(),
        encoding="utf-8",
    )

    code = main(
        [
            "historical-prd",
            "validate",
            "--manifest",
            str(manifest),
            "--allowed-root",
            str(tmp_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "VALID"
    assert payload["document_count"] == 1


def test_historical_ingest_parser_accepts_explicit_security_root_and_dsn():
    args = build_parser().parse_args(
        [
            "historical-prd",
            "ingest",
            "--manifest",
            "manifest.yaml",
            "--allowed-root",
            "/srv/prds",
            "--dsn",
            "postgresql://example",
        ]
    )

    assert args.historical_prd_command == "ingest"
    assert args.allowed_root == "/srv/prds"
