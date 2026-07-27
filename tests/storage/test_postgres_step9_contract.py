from pathlib import Path


def test_step9_migration_is_repeatable_and_enforces_binding_and_idempotency():
    migration = Path(
        "infra/local/migrations/20260727_step9_external_integrations.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS remote_repository_bindings" in migration
    assert "CREATE TABLE IF NOT EXISTS external_document_bindings" in migration
    assert "external_id_ciphertext TEXT NOT NULL" in migration
    assert "UNIQUE (owner_id, task_id, provider)" in migration
    assert "UNIQUE (owner_id, idempotency_key_hash)" in migration
    assert "CREATE TABLE IF NOT EXISTS external_investigation_runs" in migration
    assert "CREATE TABLE IF NOT EXISTS integration_call_attempts" in migration
    assert "ON CONFLICT (version) DO NOTHING" in migration
