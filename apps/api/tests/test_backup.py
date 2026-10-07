"""Backup tests use a disposable SQLite database and never touch Docker data."""

from pathlib import Path
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BackupRun, Company, FileObject, Invoice, InvoiceCompanyRole, InvoiceFileVersion, DurableTask
from app.services.backup import create_backup, verify_backup, restore_archive, _read_manifest
from app.config import settings


def test_sqlite_backup_restore_and_object_tamper_detection(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "nfe.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    objects = tmp_path / "objects"
    backups = tmp_path / "backups"
    staging = tmp_path / "staging"
    objects.mkdir(); backups.mkdir(); staging.mkdir()
    monkeypatch.setattr(settings, "objects_dir", objects)
    monkeypatch.setattr(settings, "backups_dir", backups)
    monkeypatch.setattr(settings, "staging_dir", staging)

    raw = b"<nfeProc>synthetic immutable xml</nfeProc>"
    relative = Path("ab") / ("ab" * 32 + ".xml")
    object_path = objects / relative
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(raw)
    import hashlib

    with Session() as db:
        company = Company(cnpj="12345678000199", legal_name="Synthetic", confirmed=True)
        db.add(company); db.flush()
        invoice = Invoice(environment="production", access_key="A" * 44, issuer_cnpj=company.cnpj, issue_local_date=date(2026, 7, 1), v_nf=Decimal("100.00"), fiscal_status="authorized")
        db.add(invoice); db.flush()
        db.add(InvoiceCompanyRole(invoice_id=invoice.id, company_id=company.id, role="issuer", business_class="sale", included=True, review_state="confirmed"))
        obj = FileObject(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), relative_path=relative.as_posix(), original_name="sample.xml")
        db.add(obj); db.flush()
        db.add(InvoiceFileVersion(invoice_id=invoice.id, object_id=obj.id, is_primary=True))
        pending_zip = b"synthetic queued upload"
        (staging / "pending.zip").write_bytes(pending_zip)
        db.add(DurableTask(kind="import", entity_id=1, status="queued", payload={"uploads": [{"path": "pending.zip", "name": "monthly.zip", "sha256": hashlib.sha256(pending_zip).hexdigest()}]}))
        db.commit()
        result = create_backup(db)
        assert result["status"] == "completed"
        run = db.get(BackupRun, result["id"])
        checked = verify_backup(db, run)
        assert checked["status"] == "verified"
        assert checked["sales_monthly"] == {"2026-07": "100.00"}
        assert checked["company_sales_monthly"] == {f"{company.id}:2026-07": "100.00"}
        assert checked["sample_export_count"] == 1
        assert checked["references_verified"] is True
        assert checked["staging_count"] == 1
        assert checked["staging_references_verified"] is True
        assert checked["restored_table_counts"]["invoices"] == 1
        assert checked["elapsed_seconds"] >= 0
        assert run.last_restore_test is not None

        # A safe manual recovery produces a new DB/object directory only.
        recovery_dir = tmp_path / "recovered"
        recovered = restore_archive(Path(result["path"]), recovery_dir, run.details["archive_sha256"])
        assert recovered["status"] == "recovered_to_new_target"
        assert (recovery_dir / "database.sqlite").is_file()
        assert (recovery_dir / "objects" / relative).read_bytes() == raw
        assert (recovery_dir / "staging" / "pending.zip").read_bytes() == pending_zip
        assert recovered["sample_export_count"] == 1
        assert db.get(Invoice, invoice.id).v_nf == Decimal("100.00")
        with pytest.raises(RuntimeError, match="目标目录必须尚不存在"):
            restore_archive(Path(result["path"]), recovery_dir, run.details["archive_sha256"])
        with pytest.raises(RuntimeError, match="业务数据目录"):
            restore_archive(Path(result["path"]), objects / "overwritten", run.details["archive_sha256"])

        archive = Path(result["path"])
        original_archive = archive.read_bytes()
        archive.write_bytes(original_archive + b"tamper")
        with pytest.raises(RuntimeError, match="归档自身哈希不一致"):
            verify_backup(db, run)
        archive.write_bytes(original_archive)

        # The immutable source object is protected: tampering causes backup to fail.
        object_path.write_bytes(b"tampered")
        with pytest.raises(RuntimeError, match="哈希或大小不一致"):
            create_backup(db)
        failed = db.query(BackupRun).order_by(BackupRun.id.desc()).first()
        assert failed.status == "failed"


def test_backup_archive_path_traversal_is_rejected(tmp_path):
    import zipfile

    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.xml", b"unsafe")
    extracted = tmp_path / "isolated"
    extracted.mkdir()
    with pytest.raises(RuntimeError, match="路径越界"):
        _read_manifest(archive, extracted)
    assert not (tmp_path / "escape.xml").exists()


def test_disk_full_backup_fails_atomically_and_preserves_source(tmp_path, monkeypatch):
    import errno
    import hashlib
    import app.services.backup as service

    engine = create_engine(f"sqlite:///{tmp_path / 'failure.db'}", future=True)
    Base.metadata.create_all(engine)
    objects = tmp_path / "objects"
    backups = tmp_path / "backups"
    objects.mkdir(); backups.mkdir()
    monkeypatch.setattr(settings, "objects_dir", objects)
    monkeypatch.setattr(settings, "backups_dir", backups)
    raw = b"preserve this immutable source"
    (objects / "original.xml").write_bytes(raw)
    def no_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(service.shutil, "copyfile", no_space)
    with sessionmaker(bind=engine)() as db:
        db.add(FileObject(sha256=hashlib.sha256(raw).hexdigest(), relative_path="original.xml", size_bytes=len(raw)))
        db.commit()
        with pytest.raises(OSError, match="No space left"):
            create_backup(db)
        failed = db.query(BackupRun).one()
        assert failed.status == "failed"
        assert "No space left" in failed.details["error"]
        assert db.query(FileObject).count() == 1
        assert (objects / "original.xml").read_bytes() == raw
        assert list(backups.iterdir()) == []


def test_monthly_snapshot_excludes_testing_cancelled_conflicting_and_purchase(tmp_path, monkeypatch):
    from app.services.backup import _monthly_sales

    engine = create_engine(f"sqlite:///{tmp_path / 'scope.db'}", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        company = Company(cnpj="12345678000199", confirmed=True)
        db.add(company); db.flush()
        cases = [
            ("production", "authorized", False, "sale", True, "100.25"),
            ("production", "authorized", False, "sales_return", True, "40.10"),
            ("production", "authorized", False, "purchase", True, "500.00"),
            ("testing", "authorized", False, "sale", True, "999.00"),
            ("production", "canceled", False, "sale", True, "88.00"),
            ("production", "authorized", True, "sale", True, "77.00"),
            ("production", "authorized", False, "sale", False, "66.00"),
        ]
        for number, (environment, status, conflict, classification, included, amount) in enumerate(cases):
            invoice = Invoice(access_key=str(number).zfill(44), environment=environment, fiscal_status=status, conflict=conflict, issue_local_date=date(2026, 7, 31), v_nf=Decimal(amount))
            db.add(invoice); db.flush()
            db.add(InvoiceCompanyRole(invoice_id=invoice.id, company_id=company.id, role="issuer", business_class=classification, included=included))
        db.commit()
        assert _monthly_sales(db) == {"2026-07": "60.15"}
        assert _monthly_sales(db, per_company=True) == {f"{company.id}:2026-07": "60.15"}


def test_postgres_mutation_lock_uses_dedicated_connection_across_commit_and_nested_calls():
    """Regression for a Session connection that closed after db.commit()."""
    from types import SimpleNamespace
    from app.services.locking import mutation_lock

    state = {"held": False, "opened": 0, "closed": False}

    class DedicatedConnection:
        def execution_options(self, **options):
            return self
        def exec_driver_sql(self, sql, parameters):
            if "pg_advisory_unlock" in sql:
                assert state["held"]
                state["held"] = False
                return SimpleNamespace(scalar_one=lambda: True)
            state["held"] = True
        def close(self):
            state["closed"] = True
        def invalidate(self):
            state["held"] = False

    class Engine:
        dialect = SimpleNamespace(name="postgresql")
        def connect(self):
            state["opened"] += 1
            return DedicatedConnection()

    class BusinessSession:
        def __init__(self, engine):
            self.engine = engine
        def get_bind(self):
            return self.engine
        def connection(self):
            raise AssertionError("业务 Session 连接不能用于持有备份锁")
        def commit(self):
            assert state["held"]

    db = BusinessSession(Engine())
    with mutation_lock(db):
        db.commit()
        with mutation_lock(BusinessSession(db.engine)):
            assert state["held"]
            db.commit()
        assert state["held"]
    assert state == {"held": False, "opened": 1, "closed": True}
