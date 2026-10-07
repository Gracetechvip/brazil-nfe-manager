"""Consistent, self-contained backup snapshots and isolated restore verification.

The service deliberately writes a new archive through a temporary path and only
marks a ``BackupRun`` complete after the archive has been reopened and every
object hash has been checked.  It never deletes or mutates production objects.
"""

from __future__ import annotations

import hashlib
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from importlib import metadata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from ..config import settings
from ..models import BackupRun, FileObject, Invoice, InvoiceCompanyRole, InvoiceFileVersion, ClassificationRule, AuditLog, DurableTask
from .locking import mutation_lock


_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def _relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise RuntimeError("备份对象路径格式不安全")
    result = PurePosixPath(value)
    if result.is_absolute() or ".." in result.parts or "." in result.parts or not result.parts:
        raise RuntimeError("备份对象路径越界")
    return result


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_snapshot_name(value: str) -> str:
    return _SAFE_ID.sub("-", value).strip(".-")[:100] or "snapshot"


def _database_url(db: Session):
    bind = db.get_bind()
    # str(URL) intentionally redacts its password. Keep the URL object so the
    # subprocess environment receives the real credential without printing it.
    return make_url(getattr(bind, "url", bind.engine.url))


def _dialect(db: Session) -> str:
    return _database_url(db).get_backend_name()


def _table_counts(db: Session) -> dict[str, int]:
    result: dict[str, int] = {}
    # Table names come from our declarative metadata, never from request data.
    for name in sorted(inspect(db.get_bind()).get_table_names()):
        quoted = '"' + name.replace('"', '""') + '"'
        result[name] = int(db.execute(text(f"SELECT COUNT(*) FROM {quoted}")).scalar_one())
    return result


def _monthly_sales(db: Session, per_company: bool = False) -> dict[str, str]:
    """Return the role-qualified, Decimal-safe monthly sales view for the snapshot."""
    rows = db.execute(
        select(Invoice.issue_local_date, Invoice.v_nf, InvoiceCompanyRole.business_class, InvoiceCompanyRole.company_id)
        .join(InvoiceCompanyRole, InvoiceCompanyRole.invoice_id == Invoice.id)
        .where(
            InvoiceCompanyRole.included.is_(True),
            InvoiceCompanyRole.business_class.in_(("sale", "sales_return")),
            Invoice.fiscal_status == "authorized",
            Invoice.conflict.is_(False),
            Invoice.environment == "production",
        )
    )
    totals: dict[str, Any] = {}
    from decimal import Decimal

    for issue_date, amount, business_class, company_id in rows:
        if not issue_date:
            continue
        key = (str(company_id) + ":" if per_company else "") + issue_date.strftime("%Y-%m")
        amount = Decimal(str(amount or "0"))
        totals[key] = totals.get(key, Decimal("0")) + (amount if business_class == "sale" else -amount)
    return {key: format(value.quantize(Decimal("0.01")), "f") for key, value in sorted(totals.items())}


def _safe_config(db: Session) -> dict[str, Any]:
    libraries = {}
    for package in ("fastapi", "sqlalchemy", "psycopg", "defusedxml", "pydantic-settings"):
        try:
            libraries[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            libraries[package] = "unavailable"
    return {
        "app_timezone": settings.app_timezone,
        "max_xml_bytes": settings.max_xml_bytes,
        "max_zip_bytes": settings.max_zip_bytes,
        "max_zip_members": settings.max_zip_members,
        "database_dialect": _dialect(db),
        "backup_format": 1,
        "python_version": os.sys.version.split()[0],
        "libraries": libraries,
        "parser_versions": sorted(set(db.scalars(select(Invoice.parser_version)))) ,
        "rule_versions": sorted(set(db.scalars(select(InvoiceCompanyRole.rule_version)))),
        "rules": [{"id": rule.id, "version": rule.version, "company_id": rule.company_id, "conditions": rule.conditions_json, "result_class": rule.result_class, "active": rule.active} for rule in db.scalars(select(ClassificationRule))],
        "sales_snapshot_scope": "production; included sale/sales_return; authorized; non-conflicting; issue_local_date",
    }


def _sqlite_source(db: Session) -> Path:
    url = _database_url(db)
    if not url.database or url.database == ":memory:":
        raise RuntimeError("SQLite :memory: 数据库不能进行在线持久化备份")
    source = Path(url.database)
    if not source.is_absolute():
        source = Path.cwd() / source
    if not source.exists():
        raise FileNotFoundError(f"SQLite 数据库不存在: {source}")
    return source


def _dump_sqlite(db: Session, destination: Path) -> None:
    source = _sqlite_source(db)
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
        target_conn.commit()
        check = target_conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"SQLite integrity_check 失败: {check}")
    finally:
        target_conn.close()
        source_conn.close()


def _pg_env(url) -> dict[str, str]:
    env = os.environ.copy()
    if url.password is not None:
        env["PGPASSWORD"] = str(url.password)
    return env


def _pg_args(url, executable: str) -> list[str]:
    args = [executable]
    if url.host:
        args += ["--host", url.host]
    if url.port:
        args += ["--port", str(url.port)]
    if url.username:
        args += ["--username", url.username]
    return args


def _dump_postgres(db: Session, destination: Path) -> None:
    url = _database_url(db)
    command = _pg_args(url, "pg_dump") + ["--format=custom", "--no-owner", "--file", str(destination), url.database or ""]
    try:
        completed = subprocess.run(command, env=_pg_env(url), capture_output=True, text=True, check=False, timeout=1800)
    except FileNotFoundError as exc:
        raise RuntimeError("API 容器缺少 pg_dump，请安装 postgresql-client") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "pg_dump 失败").strip()[-500:]
        raise RuntimeError(detail)


def _dump_database(db: Session, destination: Path) -> str:
    dialect = _dialect(db)
    if dialect == "sqlite":
        _dump_sqlite(db, destination)
        return "sqlite"
    if dialect == "postgresql":
        _dump_postgres(db, destination)
        return "postgresql"
    raise RuntimeError(f"不支持的数据库方言: {dialect}")


def _object_records(db: Session, staging: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    root = Path(settings.objects_dir).resolve()
    for obj in db.scalars(select(FileObject).order_by(FileObject.id)):
        relative = _relative_path(obj.relative_path)
        source = (root / Path(*relative.parts)).resolve()
        if root not in source.parents:
            raise RuntimeError(f"原文路径越界: {obj.relative_path}")
        if not source.is_file():
            raise FileNotFoundError(f"原文对象缺失: {obj.relative_path}")
        actual_hash = _sha256(source)
        if actual_hash != obj.sha256 or source.stat().st_size != obj.size_bytes:
            raise RuntimeError(f"原文对象哈希或大小不一致: {obj.sha256}")
        target = staging / "objects" / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        records.append({"id": obj.id, "sha256": obj.sha256, "size_bytes": obj.size_bytes, "relative_path": relative.as_posix(), "original_name": obj.original_name})
    return records


def _pending_uploads(db: Session) -> dict[str, dict[str, Any]]:
    uploads = {}
    for task in db.scalars(select(DurableTask).where(DurableTask.kind == "import", DurableTask.status.in_(("queued", "running", "failed")))):
        for upload in (task.payload or {}).get("uploads", []):
            path = _relative_path(upload["path"]).as_posix()
            digest = str(upload.get("sha256", ""))
            if not _DIGEST.fullmatch(digest):
                raise RuntimeError("待恢复导入任务缺少有效上传哈希")
            if path in uploads and uploads[path]["sha256"] != digest:
                raise RuntimeError("同一导入暂存路径存在哈希冲突")
            record = uploads.setdefault(path, {"relative_path": path, "sha256": digest, "task_ids": []})
            record["task_ids"].append(task.id)
    return uploads


def _staging_records(db: Session, working: Path) -> list[dict[str, Any]]:
    source_root = Path(settings.staging_dir).resolve()
    records = []
    for record in _pending_uploads(db).values():
        relative = _relative_path(record["relative_path"])
        source = (source_root / Path(*relative.parts)).resolve()
        if source_root not in source.parents or not source.is_file():
            raise RuntimeError("待恢复导入任务暂存文件缺失或路径越界")
        if _sha256(source) != record["sha256"]:
            raise RuntimeError("待恢复导入任务暂存文件哈希不一致")
        target = working / "staging" / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        records.append({**record, "size_bytes": source.stat().st_size})
    return sorted(records, key=lambda record: record["relative_path"])


def _set_details(run: BackupRun, details: dict[str, Any]) -> None:
    if hasattr(run, "details"):
        run.details = details


def _get_run(db: Session, run_or_id: int | BackupRun | None) -> BackupRun:
    if isinstance(run_or_id, BackupRun):
        return run_or_id
    if run_or_id is not None:
        run = db.get(BackupRun, int(run_or_id))
        if run is None:
            raise ValueError(f"备份任务不存在: {run_or_id}")
        return run
    snapshot_id = f"backup-{_now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run = BackupRun(snapshot_id=snapshot_id, status="running", start_at=_now())
    db.add(run)
    db.flush()
    return run


def create_backup(db: Session, run_id: int | BackupRun | None = None) -> dict[str, Any]:
    """Create and verify an atomic snapshot archive.

    ``run_id`` may be an existing BackupRun or its id.  On errors, the run is
    marked failed and the previous production database/files are left alone.
    """
    with mutation_lock(db):
        run = _get_run(db, run_id)
        if run.status == "completed":
            raise ValueError("已完成的备份不能被覆盖；请创建新的备份任务")
        run.status = "running"
        run.start_at = run.start_at or _now()
        db.commit()
        staging = Path(settings.backups_dir) / f".working-{_safe_snapshot_name(run.snapshot_id)}-{uuid.uuid4().hex}"
        archive = Path(settings.backups_dir) / f"{_safe_snapshot_name(run.snapshot_id)}.zip"
        tmp_archive = None
        elapsed_start = time.monotonic()
        try:
            if archive.exists():
                raise FileExistsError("备份归档已存在，拒绝覆盖")
            staging.mkdir(parents=True, exist_ok=False)
            database_file = staging / ("database.sqlite" if _dialect(db) == "sqlite" else "database.dump")
            dialect = _dump_database(db, database_file)
            object_records = _object_records(db, staging)
            manifest: dict[str, Any] = {
                "format": "br-nfe-backup-v1",
                "snapshot_id": run.snapshot_id,
                "created_at": _now().isoformat() + "Z",
                "database": {"dialect": dialect, "path": database_file.name, "sha256": _sha256(database_file), "size_bytes": database_file.stat().st_size},
                "table_counts": _table_counts(db),
                "sales_monthly": _monthly_sales(db),
                "company_sales_monthly": _monthly_sales(db, per_company=True),
                "objects": object_records,
                "staging": _staging_records(db, staging),
                "config": _safe_config(db),
            }
            _verify_references(db, manifest)
            (staging / "config").mkdir(exist_ok=True)
            config_path = staging / "config" / "settings.json"
            config_path.write_text(json.dumps(manifest["config"], ensure_ascii=False, indent=2), encoding="utf-8")
            manifest["config_sha256"] = _sha256(config_path)
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            tmp_archive = archive.with_suffix(f".tmp-{uuid.uuid4().hex}.zip")
            with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_STORED) as zf:
                for path in sorted(p for p in staging.rglob("*") if p.is_file()):
                    zf.write(path, path.relative_to(staging).as_posix())
            _verify_archive_file(tmp_archive, manifest, require_restore=False)
            with tmp_archive.open("r+b") as completed_file:
                os.fsync(completed_file.fileno())
            tmp_archive.replace(archive)
            run.status = "completed"
            run.manifest_path = str(archive)
            run.external_status = "not_configured"
            run.end_at = _now()
            _set_details(run, {"archive_sha256": _sha256(archive), "object_count": len(object_records), "staging_count": len(manifest["staging"]), "table_counts": manifest["table_counts"], "sales_monthly": manifest["sales_monthly"], "company_sales_monthly": manifest["company_sales_monthly"], "elapsed_seconds": round(time.monotonic() - elapsed_start, 3)})
            db.add(AuditLog(action="backup_completed", entity_type="backup", entity_id=str(run.id), after_json={"snapshot_id": run.snapshot_id, "object_count": len(object_records)}))
            db.commit()
            return {"id": run.id, "snapshot_id": run.snapshot_id, "status": run.status, "path": str(archive), "manifest": manifest}
        except Exception as exc:
            db.rollback()
            run = db.get(BackupRun, run.id)
            if run:
                run.status = "failed"
                run.end_at = _now()
                _set_details(run, {"error": str(exc)[:1000]})
                db.add(AuditLog(action="backup_failed", entity_type="backup", entity_id=str(run.id), after_json={"error": str(exc)[:1000]}))
                db.commit()
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if tmp_archive is not None:
                tmp_archive.unlink(missing_ok=True)


def _read_manifest(archive: Path, extract_dir: Path) -> dict[str, Any]:
    with zipfile.ZipFile(archive) as zf:
        seen = set()
        expanded_bytes = 0
        for info in zf.infolist():
            path = _relative_path(info.filename.rstrip("/"))
            if path.as_posix() in seen or info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16):
                raise RuntimeError("备份包包含重复、加密或符号链接成员")
            seen.add(path.as_posix())
            expanded_bytes += info.file_size
            if info.compress_size and info.file_size / info.compress_size > 500:
                raise RuntimeError("备份包压缩比超过安全上限")
        if expanded_bytes > shutil.disk_usage(extract_dir).free - 16 * 1024 * 1024:
            raise RuntimeError("隔离校验目录磁盘空间不足")
        # Stream extraction only into a freshly-created directory. No archive
        # path is passed to an OS shell or used as an absolute destination.
        for info in zf.infolist():
            destination = extract_dir / Path(*_relative_path(info.filename.rstrip("/")).parts)
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
    manifest_path = extract_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("备份包缺少 manifest.json")
    result = json.loads(manifest_path.read_text(encoding="utf-8"))
    if result.get("format") != "br-nfe-backup-v1":
        raise RuntimeError("备份格式版本不支持")
    return result


def _verify_extracted(root: Path, manifest: dict[str, Any]) -> None:
    database_name = str(manifest["database"]["path"])
    if database_name not in {"database.sqlite", "database.dump"}:
        raise RuntimeError("备份数据库文件名无效")
    database = root / database_name
    if _sha256(database) != manifest["database"]["sha256"] or database.stat().st_size != int(manifest["database"]["size_bytes"]):
        raise RuntimeError("数据库快照哈希不一致")
    if manifest.get("config_sha256") and _sha256(root / "config" / "settings.json") != manifest["config_sha256"]:
        raise RuntimeError("配置快照哈希不一致")
    object_ids = set()
    for record in manifest.get("objects", []):
        if record["id"] in object_ids or not _DIGEST.fullmatch(record["sha256"]):
            raise RuntimeError("原文清单对象身份或哈希无效")
        object_ids.add(record["id"])
        path = root / "objects" / Path(*_relative_path(record["relative_path"]).parts)
        if not path.is_file() or path.stat().st_size != int(record["size_bytes"]) or _sha256(path) != record["sha256"]:
            raise RuntimeError(f"原文对象校验失败: {record['relative_path']}")
    for record in manifest.get("staging", []):
        path = root / "staging" / Path(*_relative_path(record["relative_path"]).parts)
        if not path.is_file() or path.stat().st_size != int(record["size_bytes"]) or _sha256(path) != record["sha256"]:
            raise RuntimeError("待恢复导入任务暂存文件校验失败")


def _verify_archive_file(archive: Path, manifest: dict[str, Any], require_restore: bool, production_db: Session | None = None) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="nfe-verify-") as temp:
        root = Path(temp)
        actual_manifest = _read_manifest(archive, root)
        if actual_manifest.get("snapshot_id") != manifest.get("snapshot_id"):
            raise RuntimeError("manifest snapshot_id 不一致")
        _verify_extracted(root, actual_manifest)
        restored = _restore_isolated(root, actual_manifest, production_db) if require_restore else {}
        return {"snapshot_id": actual_manifest["snapshot_id"], "object_count": len(actual_manifest.get("objects", [])), "table_counts": actual_manifest.get("table_counts", {}), "sales_monthly": actual_manifest.get("sales_monthly", {}), "company_sales_monthly": actual_manifest.get("company_sales_monthly", {}), "elapsed_seconds": round(time.monotonic() - started, 3), **restored}


def _verify_references(db: Session, manifest: dict[str, Any]) -> None:
    actual = [{"id": obj.id, "sha256": obj.sha256, "size_bytes": obj.size_bytes, "relative_path": obj.relative_path, "original_name": obj.original_name} for obj in db.scalars(select(FileObject).order_by(FileObject.id))]
    expected = sorted(manifest["objects"], key=lambda item: item["id"])
    if actual != expected:
        raise RuntimeError("恢复后的数据库原文对象引用与备份清单不一致")
    pending = _pending_uploads(db)
    staging = {record["relative_path"]: record for record in manifest.get("staging", [])}
    if set(pending) != set(staging):
        raise RuntimeError("恢复后待重试导入任务与暂存清单不一致")
    for path, upload in pending.items():
        if staging[path]["sha256"] != upload["sha256"] or sorted(staging[path]["task_ids"]) != sorted(upload["task_ids"]):
            raise RuntimeError("恢复后导入暂存引用身份或哈希不一致")
    inspector = inspect(db.get_bind())
    quote = db.get_bind().dialect.identifier_preparer.quote
    for table in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(table):
            columns = fk.get("constrained_columns", [])
            references = fk.get("referred_columns", [])
            parent = fk.get("referred_table")
            if not columns or not parent or len(columns) != len(references):
                continue
            join = " AND ".join(f"child.{quote(c)}=parent.{quote(r)}" for c, r in zip(columns, references))
            populated = " AND ".join(f"child.{quote(c)} IS NOT NULL" for c in columns)
            sql = f"SELECT COUNT(*) FROM {quote(table)} child LEFT JOIN {quote(parent)} parent ON {join} WHERE {populated} AND parent.{quote(references[0])} IS NULL"
            if db.execute(text(sql)).scalar_one():
                raise RuntimeError(f"恢复后的外键引用缺失: {table}")


def _sample_export(db: Session, root: Path) -> dict[str, Any]:
    """Export primary originals from the restored database, then verify bytes."""
    invoice_ids = select(Invoice.id).where(Invoice.environment == "production", Invoice.id.in_(select(InvoiceFileVersion.invoice_id))).order_by(Invoice.id).limit(20)
    selected = db.execute(select(InvoiceFileVersion, Invoice).join(Invoice, Invoice.id == InvoiceFileVersion.invoice_id).where(Invoice.id.in_(invoice_ids)).order_by(Invoice.id, InvoiceFileVersion.is_primary.desc(), InvoiceFileVersion.id)).all()
    sample = []
    seen_invoices = set()
    for version, invoice in selected:
        if invoice.id in seen_invoices:
            continue
        seen_invoices.add(invoice.id)
        obj = db.get(FileObject, version.object_id)
        if obj is None:
            raise RuntimeError("恢复导出原文引用缺失")
        sample.append((version, invoice, obj))
        if len(sample) == 20:
            break
    archive = root / "restore-export.zip"
    records = []
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zf:
        for version, invoice, obj in sample:
            filename = f"invoice-{invoice.id}-version-{version.id}.xml"
            source = root / "objects" / Path(*_relative_path(obj.relative_path).parts)
            zf.write(source, filename)
            records.append({"filename": filename, "invoice_id": invoice.id, "version_id": version.id, "sha256": obj.sha256})
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["filename", "invoice_id", "version_id", "sha256"])
        writer.writeheader(); writer.writerows(records)
        zf.writestr("manifest.csv", output.getvalue())
    with zipfile.ZipFile(archive) as zf:
        for record in records:
            if hashlib.sha256(zf.read(record["filename"])).hexdigest() != record["sha256"]:
                raise RuntimeError("隔离恢复抽样导出原文字节哈希不一致")
    return {"sample_export_count": len(records), "sample_export_sha256": _sha256(archive), "sample_export_note": "已核对原始 XML 字节" if records else "数据库无可抽样的生产发票原文版本"}


def _verify_restored_database(db: Session, root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    counts = _table_counts(db)
    if counts != manifest["table_counts"]:
        mismatch = [name for name in sorted(set(counts) | set(manifest["table_counts"])) if counts.get(name) != manifest["table_counts"].get(name)]
        raise RuntimeError("恢复后表计数不一致: " + ", ".join(mismatch))
    monthly = _monthly_sales(db)
    if monthly != manifest.get("sales_monthly", {}):
        raise RuntimeError("恢复后生产月度销售金额不一致")
    if manifest.get("company_sales_monthly", {}) != _monthly_sales(db, per_company=True):
        raise RuntimeError("恢复后公司月度销售金额不一致")
    _verify_references(db, manifest)
    return {"restored_table_counts": counts, "references_verified": True, "staging_count": len(manifest.get("staging", [])), "staging_references_verified": True, "production_monthly_verified": True, **_sample_export(db, root)}


def _restore_isolated(root: Path, manifest: dict[str, Any], production_db: Session | None = None) -> dict[str, Any]:
    dialect = manifest["database"].get("dialect")
    database = root / manifest["database"]["path"]
    if dialect == "sqlite":
        conn = sqlite3.connect(database)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("恢复后的 SQLite integrity_check 失败")
        finally:
            conn.close()
        restored_engine = create_engine("sqlite:///" + database.as_posix(), future=True)
        try:
            with Session(restored_engine) as restored:
                return _verify_restored_database(restored, root, manifest)
        finally:
            restored_engine.dispose()
    if dialect != "postgresql":
        raise RuntimeError(f"不支持的恢复数据库方言: {dialect}")
    # Production PostgreSQL URL is read from the current process only.  The
    # temporary database name is random and is always dropped in finally.
    url = _database_url(production_db) if production_db is not None else make_url(settings.database_url)
    temp_db = "nfe_restore_" + uuid.uuid4().hex[:16]
    created = _pg_args(url, "createdb") + ["--maintenance-db", url.database or "postgres", "--template=template0", temp_db]
    restore = _pg_args(url, "pg_restore") + ["--exit-on-error", "--no-owner", "--no-privileges", "--dbname", temp_db, str(database)]
    dropped = _pg_args(url, "dropdb") + ["--if-exists", "--maintenance-db", url.database or "postgres", temp_db]
    database_created = False
    restored_engine = None
    try:
        for command in (created, restore):
            completed = subprocess.run(command, env=_pg_env(url), capture_output=True, text=True, check=False, timeout=1800)
            if completed.returncode:
                raise RuntimeError((completed.stderr or completed.stdout or "PostgreSQL 恢复失败").strip()[-500:])
            if command is created:
                database_created = True
        restored_engine = create_engine(url.set(database=temp_db), future=True)
        with Session(restored_engine) as restored:
            result = _verify_restored_database(restored, root, manifest)
        return {"isolated_database": temp_db, **result}
    finally:
        if restored_engine is not None:
            restored_engine.dispose()
        if database_created:
            cleaned = subprocess.run(dropped, env=_pg_env(url), capture_output=True, text=True, check=False, timeout=180)
            if cleaned.returncode:
                raise RuntimeError("隔离恢复数据库清理失败: " + temp_db)


def verify_backup(db: Session, run_id: int | BackupRun) -> dict[str, Any]:
    """Verify hashes and restore into an isolated temporary database/directory."""
    with mutation_lock(db):
        run = _get_run(db, run_id)
        if not run.manifest_path:
            raise ValueError("备份没有可验证的归档路径")
        try:
            archive_path = Path(run.manifest_path)
            saved_hash = (getattr(run, "details", {}) or {}).get("archive_sha256")
            if saved_hash and _sha256(archive_path) != saved_hash:
                raise RuntimeError("备份归档自身哈希不一致")
            result = _verify_archive_file(archive_path, {"snapshot_id": run.snapshot_id}, require_restore=True, production_db=db)
            run.last_restore_test = _now()
            _set_details(run, {**(getattr(run, "details", {}) or {}), "last_restore_test": run.last_restore_test.isoformat() + "Z", "restore_verified": True, "restore_evidence": result})
            db.add(AuditLog(action="backup_restore_verified", entity_type="backup", entity_id=str(run.id), after_json={"elapsed_seconds": result["elapsed_seconds"], "object_count": result["object_count"], "sample_export_count": result["sample_export_count"]}))
            db.commit()
            return {"id": run.id, "snapshot_id": run.snapshot_id, "status": "verified", **result}
        except Exception as exc:
            db.rollback()
            run = db.get(BackupRun, run.id)
            _set_details(run, {**(getattr(run, "details", {}) or {}), "restore_verified": False, "restore_error": str(exc)[:1000]})
            db.add(AuditLog(action="backup_restore_failed", entity_type="backup", entity_id=str(run.id), after_json={"error": str(exc)[:1000]}))
            db.commit()
            raise


def restore_archive(archive: Path, target_dir: Path, expected_sha256: str, database_name: str | None = None) -> dict[str, Any]:
    """Recover into a *new* directory/database; replacing production is forbidden.

    This administrator CLI entry does not have a ``--force`` or overwrite mode.
    PostgreSQL uses the configured connection only for creating a distinct
    ``nfe_recovered_*`` database. Credentials are read from the environment.
    """
    started = time.monotonic()
    archive = Path(archive).resolve()
    target = Path(target_dir).resolve()
    if not _DIGEST.fullmatch(expected_sha256) or _sha256(archive) != expected_sha256:
        raise RuntimeError("备份归档自身哈希不一致；请核对管理员保存的 SHA-256")
    if target.exists():
        raise RuntimeError("恢复目标目录必须尚不存在，不允许覆盖已有文件")
    for live in (settings.objects_dir, settings.staging_dir, settings.exports_dir, settings.backups_dir):
        live = Path(live).resolve()
        if target == live or live in target.parents or target in live.parents:
            raise RuntimeError("恢复目录不能位于现有业务数据目录或覆盖其父目录")
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        live_db = Path(url.database).resolve()
        if target == live_db or target in live_db.parents:
            raise RuntimeError("恢复目录不能覆盖当前 SQLite 数据库")
    if database_name and (not re.fullmatch(r"nfe_recovered_[a-z0-9_]{1,40}", database_name) or database_name == url.database):
        raise RuntimeError("恢复数据库名称必须为新的 nfe_recovered_*，不能使用生产库名")
    target.parent.mkdir(parents=True, exist_ok=True)
    created_database = False
    restored_engine = None
    try:
        with tempfile.TemporaryDirectory(prefix=".nfe-restore-", dir=target.parent) as working:
            root = Path(working)
            manifest = _read_manifest(archive, root)
            _verify_extracted(root, manifest)
            if manifest["database"]["dialect"] == "sqlite":
                if database_name:
                    raise RuntimeError("SQLite 备份不接受 PostgreSQL 数据库名称")
                evidence = _restore_isolated(root, manifest)
            elif manifest["database"]["dialect"] == "postgresql":
                if url.get_backend_name() != "postgresql" or not database_name:
                    raise RuntimeError("PostgreSQL 恢复需要 DATABASE_URL 和新的 --database-name")
                create_command = _pg_args(url, "createdb") + ["--maintenance-db", url.database or "postgres", "--template=template0", database_name]
                restore_command = _pg_args(url, "pg_restore") + ["--exit-on-error", "--no-owner", "--no-privileges", "--dbname", database_name, str(root / "database.dump")]
                for command in (create_command, restore_command):
                    completed = subprocess.run(command, env=_pg_env(url), capture_output=True, text=True, check=False, timeout=1800)
                    if completed.returncode:
                        raise RuntimeError((completed.stderr or "恢复失败").strip()[-500:])
                    if command is create_command:
                        created_database = True
                restored_engine = create_engine(url.set(database=database_name), future=True)
                with Session(restored_engine) as restored:
                    evidence = _verify_restored_database(restored, root, manifest)
                restored_engine.dispose()
                restored_engine = None
            else:
                raise RuntimeError("备份数据库方言不支持")
            result = {"status": "recovered_to_new_target", "snapshot_id": manifest["snapshot_id"], "target_dir": str(target), "database_name": database_name, "elapsed_seconds": round(time.monotonic() - started, 3), **evidence}
            (root / "restore-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            # Rename a new working directory to a new destination; never delete
            # or replace an existing directory to make restoration fit.
            root.rename(target)
            return result
    except Exception:
        if restored_engine is not None:
            restored_engine.dispose()
        if created_database:
            # Only the exact newly-created, validated recovery database is
            # eligible for cleanup after a failed restoration.
            command = _pg_args(url, "dropdb") + ["--maintenance-db", url.database or "postgres", database_name]
            cleaned = subprocess.run(command, env=_pg_env(url), capture_output=True, text=True, check=False, timeout=180)
            if cleaned.returncode:
                raise RuntimeError("恢复失败且新恢复库清理失败，请检查: " + str(database_name))
        raise


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="NF-e 完整备份的安全隔离恢复入口；不允许覆盖生产")
    subcommands = parser.add_subparsers(dest="operation", required=True)
    restore = subcommands.add_parser("restore", help="恢复到新的目录和新的数据库")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--target-dir", type=Path, required=True)
    restore.add_argument("--expected-sha256", required=True)
    restore.add_argument("--database-name", default=None)
    arguments = parser.parse_args()
    try:
        result = restore_archive(arguments.archive, arguments.target_dir, arguments.expected_sha256, arguments.database_name)
    except Exception as exc:
        parser.exit(1, "恢复失败: " + str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
