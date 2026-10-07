"""Durable import checkpoints and frozen, hash-verified export archives."""
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import csv, io, os, zipfile
from sqlalchemy import select
from fastapi import HTTPException
from ..config import settings
from ..db import SessionLocal
from ..models import (DurableTask, User, Company, ImportBatch, ImportEntry, Invoice,
    InvoiceFileVersion, FileObject, ExportJob, ExportJobFile, InvoiceEvent,
    EventFileVersion, AuditLog)
from .access import company_ids, invoice_companies, permits
from .query import invoice_query
from .importer import iter_upload, process_xml
from .locking import mutation_lock
from .stats import summarize

RESULTS = ("new", "updated", "duplicate", "conflict", "event", "unsupported", "failed")

def refresh_import(db, batch, total=None):
    latest = {}
    for e in db.scalars(select(ImportEntry).where(ImportEntry.batch_id == batch.id).order_by(ImportEntry.id)):
        latest[e.member_path] = e.result
    counts = {k: sum(v == k for v in latest.values()) for k in RESULTS}
    batch.counters = counts | {"total": max(total or 0, len(latest)), "processed": len(latest)}
    db.flush()

def run_import(db, task):
    batch = db.get(ImportBatch, task.entity_id)
    user = db.get(User, batch.created_by)
    if not user or not user.active: raise RuntimeError("导入用户已停用")
    ids = company_ids(db, user, "import")
    if ids is not None and not ids: raise RuntimeError("没有导入公司权限")
    target = db.get(Company, batch.target_company_id) if batch.target_company_id else None
    if target and not permits(db, user, target.id, "import"): raise RuntimeError("导入权限已撤销")
    allowed = None if ids is None else {c.cnpj for c in db.scalars(select(Company).where(Company.id.in_(ids)))}
    batch.status = "processing"; db.commit()
    initial = (batch.report or {}).get("before") or summarize(db,user,{})[0]
    batch.report = {**(batch.report or {}), "before": initial}; db.commit()
    latest = {}
    for e in db.scalars(select(ImportEntry).where(ImportEntry.batch_id == batch.id).order_by(ImportEntry.id)):
        latest[e.member_path] = e.result
    processed = 0
    for uindex, upload in enumerate(task.payload.get("uploads", [])):
        try:
            raw = (settings.staging_dir / upload["path"]).read_bytes()
            if sha256(raw).hexdigest() != upload["sha256"]: raise ValueError("上传暂存哈希不一致")
            for mindex,(name,member) in enumerate(iter_upload(raw,upload["name"])):
                member_key = f"{uindex+1}:{mindex+1}:{name}"[:500]
                processed += 1
                if latest.get(member_key) and latest[member_key] != "failed": continue
                try:
                    if not name.lower().endswith(".xml"):
                        db.add(ImportEntry(batch_id=batch.id,member_path=member_key,result="unsupported",error_code="非 XML 成员，未计入发票"))
                    else:
                        process_xml(db,member,member_key,batch,target,allowed_cnpjs=allowed)
                    db.flush(); refresh_import(db,batch,processed); db.commit()
                except Exception as exc:
                    db.rollback(); batch = db.get(ImportBatch,task.entity_id)
                    error = "磁盘空间不足，请释放空间后重试" if isinstance(exc,OSError) and exc.errno == 28 else str(exc)
                    db.add(ImportEntry(batch_id=batch.id,member_path=member_key,result="failed",error_code=error[:100]))
                    refresh_import(db,batch,processed); db.commit()
        except Exception as exc:
            db.rollback(); batch = db.get(ImportBatch,task.entity_id)
            member_key = f"{uindex+1}:archive:{upload['name']}"[:500]
            if latest.get(member_key) != "failed" or task.attempts > 1:
                db.add(ImportEntry(batch_id=batch.id,member_path=member_key,result="failed",error_code=str(exc)[:100])); refresh_import(db,batch); db.commit()
    refresh_import(db,batch,processed)
    after = summarize(db,user,{})[0]
    batch.report = {"before":initial,"after":after,"sales_adjustment":format(Decimal(after["sales_amount"])-Decimal(initial["sales_amount"]),".2f"),"net_adjustment":format(Decimal(after["net_amount"])-Decimal(initial["net_amount"]),".2f"),"result_total":batch.counters["processed"],"coverage_note":"按开票日期重算历史；导入成功不代表整月资料齐全。"}
    batch.status = "partial" if batch.counters["failed"] else "completed"
    batch.finished_at = datetime.utcnow()
    db.add(AuditLog(user_id=user.id,action="import_completed",entity_type="import",entity_id=str(batch.id),after_json={"counters":batch.counters,"report":batch.report})); db.commit()

def snapshot_export(db, user, data):
    rows = list(db.scalars(invoice_query(db,user,data.filters,"export")))
    if data.invoice_ids:
        wanted = set(data.invoice_ids); rows = [i for i in rows if i.id in wanted]
        if {i.id for i in rows} != wanted: raise HTTPException(403,"选中发票中包含无权限或不符合条件的资料")
    if not rows: raise HTTPException(400,"当前条件没有可导出的发票")
    job = ExportJob(created_by=user.id,filters_json=data.model_dump(),status="queued")
    db.add(job); db.flush()
    snapshots = []; manifests = []; objects_seen = set()
    for invoice in rows:
        cids = invoice_companies(db,user,invoice.id,"export")
        snapshots.append({"id":invoice.id,"access_key":invoice.access_key,"environment":invoice.environment,"issue_date":str(invoice.issue_local_date or ""),"status":invoice.fiscal_status,"company_ids":cids})
        versions = list(db.scalars(select(InvoiceFileVersion).where(InvoiceFileVersion.invoice_id == invoice.id).order_by(InvoiceFileVersion.id)))
        if not data.all_versions: versions = [v for v in versions if v.is_primary] or versions[:1]
        if not versions: raise HTTPException(409,"发票缺少原始 XML 版本，请复核")
        def add_file(obj,version_id,kind,event_id=None):
            if not obj: raise HTTPException(409,"原文对象缺失")
            identity = (invoice.id,obj.id)
            if identity in objects_seen: return
            objects_seen.add(identity)
            name = f"{invoice.environment}/{invoice.access_key}/{kind}-{version_id}-{obj.sha256[:12]}.xml"
            db.add(ExportJobFile(export_job_id=job.id,invoice_id=invoice.id,event_id=event_id,object_id=obj.id,zip_path=name))
            manifests.append({"environment":invoice.environment,"access_key":invoice.access_key,"company":';'.join(str(x) for x in cids),"issue_date":str(invoice.issue_local_date or ""),"status":invoice.fiscal_status,"filename":name,"version":version_id,"kind":kind,"sha256":obj.sha256})
        for v in versions: add_file(db.get(FileObject,v.object_id),v.id,v.document_kind)
        if data.include_events:
            for event in db.scalars(select(InvoiceEvent).where(InvoiceEvent.invoice_id==invoice.id)):
                for v in db.scalars(select(EventFileVersion).where(EventFileVersion.event_id==event.id)):
                    add_file(db.get(FileObject,v.object_id),v.id,"event",event.id)
    job.result_snapshot={"invoice_count":len(rows),"file_count":len(manifests),"created_at":datetime.utcnow().isoformat(),"expires_at":(datetime.utcnow()+timedelta(days=7)).isoformat(),"invoices":snapshots,"manifest":manifests}
    db.add(DurableTask(kind="export",entity_id=job.id))
    db.add(AuditLog(user_id=user.id,action="export_created",entity_type="export",entity_id=str(job.id),after_json={"filters":data.filters,"invoice_count":len(rows),"file_count":len(manifests)}))
    db.commit(); return job

def check_export_access(db,job,user):
    if not user or not user.active: raise HTTPException(403,"用户已停用")
    for snap in (job.result_snapshot or {}).get("invoices",[]):
        if not any(permits(db,user,cid,"export") for cid in snap["company_ids"]):
            raise HTTPException(403,"公司导出权限已变更，无法继续下载此任务")

def run_export(db,task):
    job=db.get(ExportJob,task.entity_id); user=db.get(User,job.created_by)
    check_export_access(db,job,user)
    job.status="processing"; db.commit()
    archive=settings.exports_dir/f"export-{job.id}.zip"; temp=archive.with_suffix(".tmp")
    entries=list(db.scalars(select(ExportJobFile).where(ExportJobFile.export_job_id==job.id)))
    if not entries: raise RuntimeError("没有冻结文件快照，请重新创建任务")
    try:
        with zipfile.ZipFile(temp,"w",zipfile.ZIP_DEFLATED) as z:
            for entry in entries:
                obj=db.get(FileObject,entry.object_id); path=(settings.objects_dir/obj.relative_path).resolve()
                if settings.objects_dir.resolve() not in path.parents: raise RuntimeError("原文路径越界")
                raw=path.read_bytes()
                if sha256(raw).hexdigest()!=obj.sha256 or len(raw)!=obj.size_bytes: raise RuntimeError("原文哈希或大小不一致，导出中止")
                z.writestr(entry.zip_path,raw)
            output=io.StringIO(); records=job.result_snapshot["manifest"]
            writer=csv.DictWriter(output,fieldnames=list(records[0]));writer.writeheader();writer.writerows(records)
            z.writestr("manifest.csv",output.getvalue().encode("utf-8-sig"))
        with temp.open("r+b") as fh: os.fsync(fh.fileno())
        os.replace(temp,archive)
        job.archive_path=str(archive);job.file_count=len(entries);job.status="completed";job.completed_at=datetime.utcnow()
        job.result_snapshot={**job.result_snapshot,"archive_sha256":sha256(archive.read_bytes()).hexdigest(),"size_bytes":archive.stat().st_size}
        db.commit()
    finally: temp.unlink(missing_ok=True)

def execute_task(task_id):
    with SessionLocal() as db:
        with mutation_lock(db):
            task=db.get(DurableTask,task_id)
            if not task or task.status not in {"queued","running"}: return
            task.status="running";task.attempts+=1;db.commit()
            try:
                if task.kind=="import": run_import(db,task)
                elif task.kind=="export": run_export(db,task)
                task.status="completed";task.finished_at=datetime.utcnow();task.error="";db.commit()
            except Exception as exc:
                db.rollback();task=db.get(DurableTask,task_id);task.status="failed";task.error=str(exc)[:1000];task.finished_at=datetime.utcnow()
                item=db.get(ImportBatch if task.kind=="import" else ExportJob,task.entity_id)
                if item:
                    item.status="failed"
                    if task.kind=="export": item.result_snapshot={**(item.result_snapshot or {}),"error":task.error}
                    else: item.report={**(item.report or {}),"error":task.error}
                db.commit()

def run_pending_once():
    with SessionLocal() as db:
        task_id=db.scalar(select(DurableTask.id).where(DurableTask.status.in_(["queued","running"])).order_by(DurableTask.id).limit(1))
    if task_id: execute_task(task_id)
    return bool(task_id)
