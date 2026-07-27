from pathlib import Path

from prd_agent.storage.postgres_historical import PostgresHistoricalPrdStore


class RecordingCursor:
    def __init__(self):
        self.sql = ""
        self.parameters = ()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, sql, parameters=()):
        self.sql = sql
        self.parameters = parameters

    def fetchall(self):
        return []


class RecordingConnection:
    def __init__(self):
        self.last_cursor = None

    def cursor(self):
        self.last_cursor = RecordingCursor()
        return self.last_cursor


def test_postgres_keyword_ranking_applies_owner_project_and_labels_in_sql():
    connection = RecordingConnection()
    store = PostgresHistoricalPrdStore(connection)

    result = store.keyword_ranked_entries(
        "corpus",
        "corpus-version",
        query="订单状态",
        owner_id="owner",
        project_id="project",
        access_labels=frozenset({"team"}),
        product_tags=frozenset(),
        updated_after=None,
        limit=5,
    )

    sql = connection.last_cursor.sql
    assert result == ()
    assert "d.owner_id = %s" in sql
    assert "d.project_id = %s" in sql
    assert "d.access_labels <@ %s::text[]" in sql
    assert "ts_rank_cd" in sql
    assert connection.last_cursor.parameters[-1] == 5


def test_step8_migration_is_repeatable_and_contains_source_contracts():
    migration = Path(
        "infra/local/migrations/20260727_step8_historical_prd_rag.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS historical_corpora" in migration
    assert "CREATE INDEX IF NOT EXISTS ix_historical_chunks_fts" in migration
    assert "IF NOT EXISTS" in migration
    assert "ck_source_evidence_source_contract" in migration
