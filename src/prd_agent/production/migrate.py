from __future__ import annotations

import argparse
from pathlib import Path

from prd_agent.production.config import secret_or_environment


def migration_paths(root: Path) -> tuple[Path, ...]:
    schema = root / "schema.sql"
    migrations = tuple(sorted((root / "migrations").glob("*.sql")))
    return (schema, *migrations)


def migrate(root: Path) -> None:
    import psycopg

    dsn = secret_or_environment("PRD_AGENT_DATABASE_DSN")
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(hashtext('prd-agent-migrate'))")
            try:
                cursor.execute("SELECT to_regclass('public.prd_tasks')")
                is_empty = cursor.fetchone()[0] is None
                if is_empty:
                    cursor.execute(
                        (root / "schema.sql").read_text(encoding="utf-8")
                    )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute("SELECT version FROM schema_migrations")
                applied = {row[0] for row in cursor.fetchall()}
                for path in migration_paths(root)[1:]:
                    version = path.stem
                    if version in applied:
                        continue
                    cursor.execute(path.read_text(encoding="utf-8"))
                    cursor.execute(
                        """
                        INSERT INTO schema_migrations(version)
                        VALUES (%s)
                        ON CONFLICT (version) DO NOTHING
                        """,
                        (version,),
                    )
            finally:
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtext('prd-agent-migrate'))"
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply PRD Agent migrations")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/app/infra/local"),
    )
    args = parser.parse_args(argv)
    migrate(args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
