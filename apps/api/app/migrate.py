"""Idempotent, versioned migrations for the initial deployment and staff workflow."""
from sqlalchemy import inspect, text
from .db import Base, engine
from . import models


def migrate():
    Base.metadata.create_all(engine)
    additions = {
        "import_batches": {"report": "JSON", "upload_key": "VARCHAR(180)"},
        "invoices": {"review_reason": "TEXT DEFAULT ''"},
        "backup_runs": {"details": "JSON"},
    }
    with engine.begin() as conn:
        for table, columns in additions.items():
            existing = {c["name"] for c in inspect(conn).get_columns(table)}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_import_upload_key ON import_batches(upload_key)"))
        for version in ("001_initial", "002_staff_workflows"):
            conn.execute(text("INSERT INTO schema_migrations(version,applied_at) VALUES (:v,CURRENT_TIMESTAMP) ON CONFLICT(version) DO NOTHING"), {"v": version})


if __name__ == "__main__":
    migrate()
