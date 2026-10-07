from datetime import datetime, timedelta, date
from decimal import Decimal
from pathlib import Path
from typing import Optional
import json, shutil, uuid, zipfile, csv, io, os
from hashlib import sha256
from urllib.parse import urlparse
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_, or_, text
from sqlalchemy.orm import Session
from .db import Base, engine, get_db, SessionLocal
from .config import settings
from .models import *
from .services.security import hash_password, verify_password, new_token
from .services.importer import process_xml, iter_upload, normalize_cnpj
from .services.backup import create_backup, verify_backup
from .services.locking import mutation_lock
from .services.access import company_ids, permits, require_invoice
from .services.query import invoice_query as scoped_query, item_conditions, CLASSES
from .services.stats import summarize
from .services.jobs import snapshot_export, check_export_access
from .migrate import migrate

app = FastAPI(title="Brazil NF-e Manager", version="1.0.0")
@app.middleware("http")
async def same_origin_writes(request, call_next):
    origin = request.headers.get("origin")
    if request.method in {"POST", "PATCH", "DELETE", "PUT"} and origin and urlparse(origin).netloc != request.headers.get("host"):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=403, content={"detail": "跨站写入请求被拒绝"})
    return await call_next(request)


@app.on_event("startup")
def startup():
    settings.prepare_dirs(); migrate()


def mutation_db():
    with SessionLocal() as db:
        with mutation_lock(db):
            yield db


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(settings.session_cookie) or request.headers.get("X-Session-Token")
    if not token: raise HTTPException(401, "请先登录")
    session = db.get(Session, token)
    if not session or session.expires_at < datetime.utcnow(): raise HTTPException(401, "登录已过期")
    user = db.get(User, session.user_id)
    if not user or not user.active: raise HTTPException(401, "用户已停用")
    return user


def allowed_company_ids(db: Session, user: User) -> set[int] | None:
    return company_ids(db, user)


def can_company(db: Session, user: User, company_id: int) -> bool:
    return permits(db, user, company_id)


class LoginIn(BaseModel): login: str; password: str
class BootstrapIn(BaseModel): login: str; password: str = Field(min_length=10); display_name: str = "管理员"
class CompanyIn(BaseModel): cnpj: str; legal_name: str = ""; alias: str = ""; uf: Optional[str] = None; timezone: str = "America/Sao_Paulo"; confirmed: bool = False
class ClassificationIn(BaseModel): business_class: str; reason: str = Field(min_length=1); company_id: Optional[int] = None
class ExportIn(BaseModel): filters: dict = {}; invoice_ids: list[int] = []; include_events: bool = False; all_versions: bool = False
class PasswordIn(BaseModel): old_password: str; new_password: str = Field(min_length=10)
class CompanyConfirmIn(BaseModel): reason: str = Field(min_length=1)
class CoverageIn(BaseModel): coverage_state: str; notes: str = ""
class UserIn(BaseModel): login: str; display_name: str = ""; role: str = "readonly"; password: Optional[str] = None; active: bool = True; scopes: list[dict] = []
class RuleIn(BaseModel): version: str = "1.0"; company_id: Optional[int] = None; conditions_json: dict = {}; result_class: str = "pending"; active: bool = True
class BulkClassificationIn(ClassificationIn):
    invoice_ids: list[int] = Field(default_factory=list)
    filters: dict = Field(default_factory=dict)
    all_results: bool = False


@app.get("/api/health/live")
def live(): return {"status": "ok"}


@app.get("/api/health/ready")
def ready(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1")); return {"status": "ready"}


@app.post("/api/auth/bootstrap")
def bootstrap(data: BootstrapIn, response: Response, db: Session = Depends(mutation_db)):
    if db.scalar(select(User.id)): raise HTTPException(409, "管理员已初始化")
    user = User(login=data.login, password_hash=hash_password(data.password), display_name=data.display_name, role="admin")
    db.add(user); db.commit(); return {"created": True}


@app.post("/api/auth/login")
def login(data: LoginIn, response: Response, db: Session = Depends(mutation_db)):
    user = db.scalar(select(User).where(User.login == data.login))
    if not user or not user.active or not verify_password(data.password, user.password_hash): raise HTTPException(401, "账号或密码错误")
    token = new_token(); db.add(Session(token=token, user_id=user.id, expires_at=datetime.utcnow() + timedelta(hours=12))); db.commit()
    response.set_cookie(settings.session_cookie, token, httponly=True, samesite="lax", max_age=43200)
    return {"id": user.id, "login": user.login, "display_name": user.display_name, "role": user.role}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(mutation_db)):
    token = request.cookies.get(settings.session_cookie)
    if token:
        item = db.get(Session, token)
        if item: db.delete(item); db.commit()
    response.delete_cookie(settings.session_cookie); return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    scopes = db.scalars(select(UserCompanyScope).where(UserCompanyScope.user_id == user.id)).all()
    return {"id": user.id, "login": user.login, "display_name": user.display_name, "role": user.role, "scopes": [{"company_id": s.company_id, "permissions": s.permissions.split(",")} for s in scopes]}


@app.patch("/api/me/password")
def change_password(data: PasswordIn, user: User = Depends(current_user), db: Session = Depends(mutation_db)):
    if not verify_password(data.old_password, user.password_hash): raise HTTPException(400, "旧密码不正确")
    target=db.get(User,user.id); target.password_hash = hash_password(data.new_password); db.commit(); return {"ok": True}


@app.get("/api/companies")
def companies(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = allowed_company_ids(db, user); q = select(Company).order_by(Company.cnpj)
    if ids is not None: q = q.where(Company.id.in_(ids))
    output = []
    for c in db.scalars(q):
        rows = db.execute(select(InvoiceCompanyRole, Invoice).join(Invoice, Invoice.id == InvoiceCompanyRole.invoice_id).where(InvoiceCompanyRole.company_id == c.id)).all()
        stats, _, _ = summarize(db,user,{"company_id":c.id})
        sales = stats["sales_amount"]
        pending = stats["pending_count"]
        output.append({"id": c.id, "cnpj": c.cnpj, "legal_name": c.legal_name, "alias": c.alias, "uf": c.uf, "confirmed": c.confirmed, "active": c.active, "timezone": c.timezone, "invoice_count": len(rows), "total_sales": str(sales), "pending_count": pending})
    return output


@app.post("/api/companies")
def create_company(data: CompanyIn, user: User = Depends(current_user), db: Session = Depends(mutation_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可维护公司")
    cnpj = normalize_cnpj(data.cnpj)
    if len(cnpj)!=14: raise HTTPException(422,"CNPJ 须为14位字母数字字符串")
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try: ZoneInfo(data.timezone)
    except ZoneInfoNotFoundError: raise HTTPException(422,"公司时区无效")
    if db.scalar(select(Company).where(Company.cnpj == cnpj)): raise HTTPException(409, "CNPJ 已存在")
    company = Company(cnpj=cnpj, legal_name=data.legal_name, alias=data.alias, uf=data.uf, timezone=data.timezone, confirmed=data.confirmed); db.add(company); db.flush()
    db.add(AuditLog(user_id=user.id,action="company_created",entity_type="company",entity_id=str(company.id),after_json={"cnpj":cnpj,"confirmed":company.confirmed})); db.commit(); db.refresh(company)
    return {"id": company.id, "cnpj": company.cnpj}


@app.patch("/api/companies/{company_id:int}")
def update_company(company_id: int, data: CompanyIn, user: User = Depends(current_user), db: Session = Depends(mutation_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可维护公司")
    company = db.get(Company, company_id)
    if not company: raise HTTPException(404, "公司不存在")
    if normalize_cnpj(data.cnpj)!=company.cnpj: raise HTTPException(409,"已有公司 CNPJ 身份不可直接改写，请另建公司")
    old={"alias":company.alias,"confirmed":company.confirmed}
    company.legal_name = data.legal_name; company.alias = data.alias; company.uf = data.uf; company.timezone = data.timezone
    db.add(AuditLog(user_id=user.id,action="company_updated",entity_type="company",entity_id=str(company_id),before_json=old,after_json={"alias":company.alias})); db.commit()
    return {"id": company.id, "cnpj": company.cnpj, "confirmed": company.confirmed}


@app.post("/api/companies/{company_id:int}/confirm")
def confirm_company(company_id: int, data: CompanyConfirmIn, user: User = Depends(current_user), db: Session = Depends(mutation_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可确认公司")
    if not data.reason.strip(): raise HTTPException(422, "请填写确认依据")
    company = db.get(Company, company_id)
    if not company: raise HTTPException(404, "公司不存在")
    company.confirmed = True; db.flush()
    from .services.importer import classification_decision
    from .services.xml_parser import parse_xml
    for role, invoice in db.execute(select(InvoiceCompanyRole, Invoice).join(Invoice, Invoice.id == InvoiceCompanyRole.invoice_id).where(InvoiceCompanyRole.company_id == company_id)).all():
        if role.review_state == "manual": continue
        version = db.scalar(select(InvoiceFileVersion).where(InvoiceFileVersion.invoice_id == invoice.id, InvoiceFileVersion.is_primary == True))
        if version:
            obj = db.get(FileObject, version.object_id)
            parsed = parse_xml((settings.objects_dir / obj.relative_path).read_bytes())
            cls, included, rule_version = classification_decision(parsed, company, invoice, db)
            role.business_class = cls; role.included = included; role.rule_version = rule_version
            role.review_state = "confirmed" if included else "pending"
    db.add(AuditLog(user_id=user.id, action="company_confirm", entity_type="company", entity_id=str(company_id), after_json={"reason": data.reason})); db.commit()
    return {"ok": True, "company_id": company_id}


@app.get("/api/coverage")
def coverage(company_id: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = allowed_company_ids(db, user); q = select(CoveragePeriod).order_by(CoveragePeriod.month.desc())
    if ids is not None: q = q.where(CoveragePeriod.company_id.in_(ids))
    if company_id is not None:
        if not can_company(db, user, company_id): raise HTTPException(403, "无权访问该公司")
        q = q.where(CoveragePeriod.company_id == company_id)
    return [{"id": x.id, "company_id": x.company_id, "month": x.month, "coverage_state": x.coverage_state, "notes": x.notes, "confirmed_at": x.confirmed_at.isoformat() if x.confirmed_at else None} for x in db.scalars(q)]


@app.patch("/api/companies/{company_id:int}/coverage/{month}")
def update_coverage(company_id: int, month: str, data: CoverageIn, user: User = Depends(current_user), db: Session = Depends(mutation_db)):
    if not permits(db,user,company_id,"classify"): raise HTTPException(403,"无权维护覆盖范围")
    try: date.fromisoformat(month+"-01")
    except ValueError: raise HTTPException(422,"月份格式须为 YYYY-MM")
    if data.coverage_state not in {"not_imported","partial","complete"}: raise HTTPException(422,"覆盖状态无效")
    item = db.scalar(select(CoveragePeriod).where(CoveragePeriod.company_id == company_id, CoveragePeriod.month == month))
    if not item: item = CoveragePeriod(company_id=company_id, month=month); db.add(item)
    item.coverage_state = data.coverage_state; item.notes = data.notes; item.confirmed_by = user.id; item.confirmed_at = datetime.utcnow(); db.commit(); return {"ok": True}


@app.post("/api/imports")
async def import_files(request: Request, files: list[UploadFile] = File(...), target_company_id: Optional[int] = Form(None), user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = company_ids(db,user,"import")
    if ids is not None and not ids: raise HTTPException(403,"没有导入权限")
    target = db.get(Company,target_company_id) if target_company_id else None
    if target_company_id and not target: raise HTTPException(404,"目标公司不存在")
    if target and not permits(db,user,target.id,"import"): raise HTTPException(403,"没有该公司导入权限")
    key=request.headers.get("Idempotency-Key")
    upload_key=f"{user.id}:{key}" if key else None
    if key and len(key)>120: raise HTTPException(422,"幂等键过长")
    if upload_key:
        old=db.scalar(select(ImportBatch).where(ImportBatch.upload_key==upload_key))
        if old: return {"id":old.id,"status":old.status,"counters":old.counters}
    staged=[]
    try:
        for upload in files:
            filename=Path((upload.filename or "upload.xml").replace('\\','/')).name
            if Path(filename).suffix.lower() not in {".xml",".zip"}: raise HTTPException(422,"只支持 XML 或 ZIP")
            path=settings.staging_dir/(uuid.uuid4().hex+Path(filename).suffix.lower())
            limit=settings.max_zip_bytes if filename.lower().endswith('.zip') else settings.max_xml_bytes
            length=0
            digest=sha256()
            try:
                with path.open('wb') as fh:
                    while chunk:=await upload.read(1024*1024):
                        length+=len(chunk)
                        if length>limit: raise HTTPException(413,"文件超过上传大小上限")
                        fh.write(chunk);digest.update(chunk)
                    fh.flush();os.fsync(fh.fileno())
            except Exception:
                path.unlink(missing_ok=True);raise
            staged.append({"path":path.name,"name":filename,"sha256":digest.hexdigest()})
        if not staged: raise HTTPException(422,"请选择文件")
        with mutation_lock(db):
            if upload_key:
                old=db.scalar(select(ImportBatch).where(ImportBatch.upload_key==upload_key))
                if old:
                    for f in staged: (settings.staging_dir/f['path']).unlink(missing_ok=True)
                    return {"id":old.id,"status":old.status,"counters":old.counters}
            batch=ImportBatch(created_by=user.id,target_company_id=target.id if target else None,upload_key=upload_key,status="queued",counters={"total":0,"processed":0})
            db.add(batch);db.flush();db.add(DurableTask(kind="import",entity_id=batch.id,payload={"uploads":staged}))
            db.add(AuditLog(user_id=user.id,action="import_queued",entity_type="import",entity_id=str(batch.id),after_json={"file_count":len(staged)}));db.commit()
        return {"id":batch.id,"status":batch.status,"counters":batch.counters}
    except Exception:
        for f in staged: (settings.staging_dir/f['path']).unlink(missing_ok=True)
        raise

@app.post("/api/imports/{batch_id:int}/retry")
def retry_import(batch_id:int,user:User=Depends(current_user),db:Session=Depends(mutation_db)):
    batch=db.get(ImportBatch,batch_id)
    if not batch or batch.created_by!=user.id and user.role!="admin": raise HTTPException(404,"导入批次不存在")
    task=db.scalar(select(DurableTask).where(DurableTask.kind=="import",DurableTask.entity_id==batch_id))
    if not task: raise HTTPException(409,"旧批次没有上传暂存，请重新上传，已有发票会自动去重")
    if task.status in {"queued","running"}: raise HTTPException(409,"任务仍在处理中")
    task.status="queued";task.error="";batch.status="queued";db.commit();return {"id":batch.id,"status":"queued"}


@app.get("/api/imports")
def list_imports(user: User = Depends(current_user), db: Session = Depends(get_db)):
    q=select(ImportBatch).order_by(ImportBatch.id.desc()).limit(100)
    if user.role!="admin": q=q.where(ImportBatch.created_by==user.id)
    return [{"id":b.id,"status":b.status,"counters":b.counters,"created_at":b.created_at.isoformat(),"finished_at":b.finished_at.isoformat() if b.finished_at else None} for b in db.scalars(q)]


@app.get("/api/imports/{batch_id:int}/entries")
def import_entries(batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.created_by != user.id and user.role != "admin": raise HTTPException(404, "导入批次不存在")
    return [{"id": e.id, "member_path": e.member_path, "result": e.result, "error_code": e.error_code, "invoice_id": e.invoice_id, "event_id": e.event_id} for e in db.scalars(select(ImportEntry).where(ImportEntry.batch_id == batch_id).order_by(ImportEntry.id))]


@app.get("/api/imports/{batch_id:int}")
def import_detail(batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.created_by != user.id and user.role != "admin": raise HTTPException(404, "导入批次不存在")
    return {"id": batch.id, "status": batch.status, "counters": batch.counters, "report": batch.report or {}, "target_company_id": batch.target_company_id, "created_at": batch.created_at.isoformat(), "finished_at": batch.finished_at.isoformat() if batch.finished_at else None}


@app.get("/api/imports/{batch_id:int}/report.csv")
def import_report_csv(batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.created_by != user.id and user.role != "admin": raise HTTPException(404, "导入批次不存在")
    output = io.StringIO(); writer = csv.writer(output); writer.writerow(["member_path", "result", "error_code", "invoice_id", "event_id"])
    for e in db.scalars(select(ImportEntry).where(ImportEntry.batch_id == batch_id).order_by(ImportEntry.id)): writer.writerow([e.member_path, e.result, e.error_code or "", e.invoice_id or "", e.event_id or ""])
    return StreamingResponse(iter([output.getvalue().encode("utf-8-sig")]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="import-{batch_id:int}-report.csv"'})


def invoice_query(db: Session,user:User,params:dict):
    return scoped_query(db,user,params)


@app.get("/api/invoices")
def invoices(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db), page:int=1,page_size:int=50):
    if page<1 or page_size<1 or page_size>200: raise HTTPException(422,"分页必须为正数，每页最多200张")
    params=dict(request.query_params);stmt=invoice_query(db,user,params)
    total=db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    amount=db.scalar(select(func.sum(Invoice.v_nf)).where(Invoice.id.in_(stmt.with_only_columns(Invoice.id).order_by(None)))) or Decimal(0)
    match=select(func.sum(InvoiceItem.v_prod)).where(InvoiceItem.invoice_id.in_(stmt.with_only_columns(Invoice.id).order_by(None)),*item_conditions(params))
    matched=db.scalar(match) or Decimal(0)
    rows=list(db.scalars(stmt.offset((page-1)*page_size).limit(page_size)))
    return {"items":[invoice_out(db,i,user) for i in rows],"page":page,"page_size":page_size,"total":total,"invoice_amount":format(amount,".2f"),"matched_items_amount":format(matched,".2f")}


def invoice_out(db: Session, i: Invoice, user: User):
    roles = db.scalars(select(InvoiceCompanyRole).where(InvoiceCompanyRole.invoice_id == i.id)).all()
    ids = allowed_company_ids(db, user); roles = [r for r in roles if ids is None or r.company_id in ids]
    items = db.scalars(select(InvoiceItem).where(InvoiceItem.invoice_id == i.id).order_by(InvoiceItem.line_no)).all()
    versions = []
    for v in db.scalars(select(InvoiceFileVersion).where(InvoiceFileVersion.invoice_id == i.id).order_by(InvoiceFileVersion.id)):
        obj = db.get(FileObject, v.object_id)
        versions.append({"id": v.id, "document_kind": v.document_kind, "evidence_level": v.evidence_level, "is_primary": v.is_primary, "sha256": obj.sha256 if obj else None, "size_bytes": obj.size_bytes if obj else None, "download": f"/api/invoices/{i.id}/versions/{v.id}/download"})
    return {"id": i.id, "access_key": i.access_key, "environment": i.environment, "model": i.model, "issuer_cnpj": i.issuer_cnpj, "recipient_cnpj": i.recipient_cnpj, "number": i.number, "series": i.series, "issue_local_date": i.issue_local_date.isoformat() if i.issue_local_date else None, "fiscal_status": i.fiscal_status, "v_nf": str(i.v_nf), "v_prod": str(i.v_prod), "conflict": i.conflict, "review_reason": i.review_reason, "roles": [{"company_id": r.company_id, "role": r.role, "business_class": r.business_class, "included": r.included} for r in roles], "items": [{"line_no": x.line_no, "c_prod": x.c_prod, "x_prod": x.x_prod, "ncm": x.ncm, "cfop": x.cfop, "q_com": str(x.q_com), "v_prod": str(x.v_prod)} for x in items], "versions": versions}


@app.get("/api/invoices/{invoice_id:int}")
def invoice_detail(invoice_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    i = db.get(Invoice, invoice_id)
    if not i: raise HTTPException(404, "发票不存在")
    roles = db.scalars(select(InvoiceCompanyRole).where(InvoiceCompanyRole.invoice_id == i.id)).all()
    if not any(can_company(db, user, r.company_id) for r in roles): raise HTTPException(403, "无权访问")
    return invoice_out(db, i, user) | {"events": [{"event_type": e.event_type, "sequence": e.sequence, "accepted": e.accepted, "receipt_status": e.receipt_status, "protocol": e.protocol} for e in db.scalars(select(InvoiceEvent).where(InvoiceEvent.invoice_id == i.id))]}


@app.get("/api/invoices/{invoice_id:int}/versions/{version_id:int}/download")
def invoice_version_download(invoice_id: int, version_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id); version = db.get(InvoiceFileVersion, version_id)
    if not invoice or not version or version.invoice_id != invoice_id: raise HTTPException(404, "原始版本不存在")
    roles = db.scalars(select(InvoiceCompanyRole).where(InvoiceCompanyRole.invoice_id == invoice_id)).all()
    require_invoice(db,user,invoice_id,"export")
    obj = db.get(FileObject, version.object_id); path = settings.objects_dir / obj.relative_path if obj else None
    if not obj or not path or not path.is_file(): raise HTTPException(404, "原始文件缺失")
    if sha256(path.read_bytes()).hexdigest()!=obj.sha256: raise HTTPException(409,"原始XML哈希不一致")
    return FileResponse(path, filename=f"{invoice.access_key}-v{version.id}.xml", media_type="application/xml")


def apply_classification(db,user,invoice,role,data):
    if data.business_class not in CLASSES: raise HTTPException(422,"业务分类无效")
    if not data.reason.strip(): raise HTTPException(422,"分类必须填写依据")
    if data.business_class == "sale" and role.role!="issuer": raise HTTPException(422,"本公司不是销售方，不能标为销售")
    company=db.get(Company,role.company_id)
    if not company.confirmed: raise HTTPException(409,"请管理员先确认该公司属于管理范围")
    old={"business_class":role.business_class,"included":role.included}
    role.business_class=data.business_class;role.review_state="manual";role.rule_version="manual"
    role.included=data.business_class in {"sale","sales_return"} and invoice.fiscal_status=="authorized" and not invoice.conflict
    invoice.review_reason=data.reason
    db.add(AuditLog(user_id=user.id,action="classification",entity_type="invoice",entity_id=str(invoice.id),before_json=old,after_json={"business_class":data.business_class,"reason":data.reason,"company_id":role.company_id}))

@app.patch("/api/invoices/{invoice_id:int}/classification")
def classify_invoice(invoice_id:int,data:ClassificationIn,user:User=Depends(current_user),db:Session=Depends(mutation_db)):
    invoice=db.get(Invoice,invoice_id)
    if not invoice: raise HTTPException(404,"发票不存在")
    ids=require_invoice(db,user,invoice_id,"classify")
    roles=[r for r in db.scalars(select(InvoiceCompanyRole).where(InvoiceCompanyRole.invoice_id==invoice_id)) if r.company_id in ids and (data.company_id is None or r.company_id==data.company_id)]
    if len(roles)!=1: raise HTTPException(422,"请明确选择本次分类的公司")
    apply_classification(db,user,invoice,roles[0],data);db.commit();return {"ok":True}

@app.post("/api/invoices/classification")
def classify_invoices(data:BulkClassificationIn,user:User=Depends(current_user),db:Session=Depends(mutation_db)):
    if not data.company_id: raise HTTPException(422,"批量分类必须选择公司")
    if not permits(db,user,data.company_id,"classify"): raise HTTPException(403,"没有该公司分类权限")
    filters={**data.filters,"company_id":data.company_id}
    q=scoped_query(db,user,filters,"classify")
    if not data.all_results:
        if not data.invoice_ids: raise HTTPException(422,"请选择发票")
        q=q.where(Invoice.id.in_(data.invoice_ids))
    rows=list(db.scalars(q))
    if not data.all_results and {i.id for i in rows}!=set(data.invoice_ids): raise HTTPException(403,"选中发票含越权或不符合条件的发票")
    for invoice in rows:
        roles=list(db.scalars(select(InvoiceCompanyRole).where(InvoiceCompanyRole.invoice_id==invoice.id,InvoiceCompanyRole.company_id==data.company_id)))
        if len(roles)!=1: raise HTTPException(422,"发票公司角色不唯一，需要逐张复核")
        apply_classification(db,user,invoice,roles[0],data)
    db.commit();return {"ok":True,"changed":len(rows)}


@app.post("/api/invoices/{invoice_id:int}/resolve-conflict")
def resolve_conflict(invoice_id: int, data: CompanyConfirmIn, user: User = Depends(current_user), db: Session = Depends(mutation_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可处理冲突")
    invoice = db.get(Invoice, invoice_id)
    if not invoice: raise HTTPException(404, "发票不存在")
    if not data.reason.strip(): raise HTTPException(422,"请填写保留原版的依据")
    version=db.scalar(select(InvoiceFileVersion).where(InvoiceFileVersion.invoice_id==invoice_id,InvoiceFileVersion.is_primary==True))
    if not version: raise HTTPException(409,"没有可核对的原始主版本")
    from .services.xml_parser import parse_xml
    obj=db.get(FileObject,version.object_id); raw=(settings.objects_dir/obj.relative_path).read_bytes()
    if sha256(raw).hexdigest()!=obj.sha256: raise HTTPException(409,"主版本原文哈希异常")
    invoice.conflict = False; invoice.review_reason = data.reason; invoice.fiscal_status=parse_xml(raw).fiscal_status
    if db.scalar(select(InvoiceEvent.id).where(InvoiceEvent.invoice_id==invoice_id,InvoiceEvent.event_type=="110111",InvoiceEvent.accepted==True)): invoice.fiscal_status="canceled"
    for r in db.scalars(select(InvoiceCompanyRole).where(InvoiceCompanyRole.invoice_id==invoice_id)):
        r.included=r.business_class in {"sale","sales_return"} and invoice.fiscal_status=="authorized"
    db.add(AuditLog(user_id=user.id, action="resolve_conflict", entity_type="invoice", entity_id=str(invoice_id), after_json={"reason": data.reason})); db.commit(); return {"ok": True}


@app.get("/api/stats/summary")
def stats_summary(request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    params=dict(request.query_params);summary,_,_=summarize(db,user,params)
    full,_,_=summarize(db,user,{k:v for k,v in params.items() if k not in {"date_from","date_to"}})
    return summary|{"all_history_amount":full["net_amount"],"all_history_sales":full["sales_amount"],"history_start":full["history_start"]}

@app.get("/api/stats/trends")
def stats_trends(request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    params=dict(request.query_params)
    if params.get("granularity","month") not in {"day","week","month","year"}: raise HTTPException(422,"统计粒度无效")
    return summarize(db,user,params)[1]

@app.get("/api/stats/companies")
def stats_companies(request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    return summarize(db,user,dict(request.query_params))[2]

@app.get("/api/stats/report.csv")
def stats_csv(request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    summary,trends,ranking=summarize(db,user,dict(request.query_params))
    stream=io.StringIO();writer=csv.writer(stream);writer.writerow(["metric","amount"])
    for k,v in summary.items(): writer.writerow([k,v])
    writer.writerow([]);writer.writerow(["period","net_amount"])
    for row in trends: writer.writerow([row["period"],row["amount"]])
    return StreamingResponse(iter([stream.getvalue().encode('utf-8-sig')]),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="stats.csv"'})

@app.post("/api/exports")
def create_export(data:ExportIn,user:User=Depends(current_user),db:Session=Depends(mutation_db)):
    if company_ids(db,user,"export")==set(): raise HTTPException(403,"没有导出权限")
    job=snapshot_export(db,user,data);return {"id":job.id,"status":job.status}

def export_out(db,job):
    snapshot=job.result_snapshot or {};changes=[]
    for s in snapshot.get("invoices",[]):
        current=db.get(Invoice,s["id"])
        if current and current.fiscal_status!=s["status"]: changes.append(s["access_key"])
    return {"id":job.id,"status":job.status,"file_count":job.file_count,"snapshot":snapshot,"created_at":job.created_at.isoformat(),"completed_at":job.completed_at.isoformat() if job.completed_at else None,"changes_since_snapshot":changes,"download":f"/api/exports/{job.id}/download" if job.status=="completed" else None}

@app.get("/api/exports")
def exports(user:User=Depends(current_user),db:Session=Depends(get_db)):
    q=select(ExportJob).order_by(ExportJob.id.desc()).limit(100)
    if user.role!="admin": q=q.where(ExportJob.created_by==user.id)
    return [export_out(db,j) for j in db.scalars(q)]

@app.get("/api/exports/{job_id:int}")
def export_status(job_id:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
    job=db.get(ExportJob,job_id)
    if not job or job.created_by!=user.id and user.role!="admin": raise HTTPException(404,"导出任务不存在")
    check_export_access(db,job,user);return export_out(db,job)

@app.post("/api/exports/{job_id:int}/retry")
def retry_export(job_id:int,user:User=Depends(current_user),db:Session=Depends(mutation_db)):
    job=db.get(ExportJob,job_id)
    if not job or job.created_by!=user.id and user.role!="admin": raise HTTPException(404,"任务不存在")
    check_export_access(db,job,user)
    task=db.scalar(select(DurableTask).where(DurableTask.kind=="export",DurableTask.entity_id==job_id))
    if not task: raise HTTPException(409,"旧任务需重新创建")
    if task.status in {"queued","running"}: raise HTTPException(409,"任务正在处理")
    task.status="queued";job.status="queued";db.commit();return {"id":job.id,"status":"queued"}

@app.get("/api/exports/{job_id:int}/download")
def export_download(job_id:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
    job=db.get(ExportJob,job_id)
    if not job or job.created_by!=user.id and user.role!="admin" or job.status!="completed" or not job.archive_path: raise HTTPException(404,"导出文件不可用")
    check_export_access(db,job,user)
    expiry=(job.result_snapshot or {}).get("expires_at")
    if expiry and datetime.fromisoformat(expiry)<datetime.utcnow(): raise HTTPException(410,"导出已过期，请重新创建")
    path=Path(job.archive_path)
    if not path.is_file(): raise HTTPException(409,"导出文件缺失，请重试")
    expected=(job.result_snapshot or {}).get("archive_sha256")
    if not expected or sha256(path.read_bytes()).hexdigest()!=expected: raise HTTPException(409,"导出文件缺少哈希或已变更，请重新导出")
    return FileResponse(path,filename=path.name,media_type="application/zip")


@app.post("/api/backups")
def backup(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可备份")
    try:
        result = create_backup(db)
        return {"id": result["id"], "snapshot_id": result["snapshot_id"], "status": result["status"], "path": result["path"], "details": result.get("manifest", {}).get("table_counts", {})}
    except Exception as exc:
        raise HTTPException(500, f"备份失败：{str(exc)[:300]}") from exc


@app.get("/api/backups")
def backups(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可查看备份")
    return [{"id": b.id, "snapshot_id": b.snapshot_id, "status": b.status, "manifest_path": b.manifest_path, "start_at": b.start_at.isoformat(), "end_at": b.end_at.isoformat() if b.end_at else None, "details": b.details or {}} for b in db.scalars(select(BackupRun).order_by(BackupRun.id.desc()))]


@app.get("/api/backups/{backup_id:int}/download")
def backup_download(backup_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可下载备份")
    item = db.get(BackupRun, backup_id)
    if not item or not item.manifest_path or not Path(item.manifest_path).is_file(): raise HTTPException(404, "备份文件不存在")
    return FileResponse(item.manifest_path, filename=Path(item.manifest_path).name, media_type="application/zip")


@app.post("/api/backups/{backup_id:int}/verify")
def backup_verify(backup_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可验证备份")
    try: return verify_backup(db, backup_id)
    except Exception as exc: raise HTTPException(500, f"恢复验证失败：{str(exc)[:300]}") from exc


@app.get("/api/audit")
def audit(limit: int = 100, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可查看审计")
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500))).all()
    return {"items": [{"id": x.id, "action": x.action, "entity_type": x.entity_type, "entity_id": x.entity_id, "before": x.before_json, "after": x.after_json, "created_at": x.created_at.isoformat()} for x in rows], "total": len(rows)}


@app.get("/api/users")
def users(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可管理用户")
    output = []
    for x in db.scalars(select(User).order_by(User.login)):
        scopes = db.scalars(select(UserCompanyScope).where(UserCompanyScope.user_id == x.id)).all()
        output.append({"id": x.id, "login": x.login, "display_name": x.display_name, "role": x.role, "active": x.active, "scopes": [{"company_id": s.company_id, "permissions": s.permissions.split(",")} for s in scopes]})
    return output


@app.post("/api/users")
def create_user(data: UserIn, user: User = Depends(current_user), db: Session = Depends(mutation_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可管理用户")
    validate_user_scopes(db,data)
    if not data.password or len(data.password) < 10: raise HTTPException(400, "新用户密码至少 10 位")
    if db.scalar(select(User).where(User.login == data.login)): raise HTTPException(409, "登录名已存在")
    target = User(login=data.login, display_name=data.display_name, role=data.role, active=data.active, password_hash=hash_password(data.password)); db.add(target); db.flush()
    for scope in data.scopes: db.add(UserCompanyScope(user_id=target.id, company_id=int(scope["company_id"]), permissions=",".join(scope.get("permissions", ["view"]))))
    db.add(AuditLog(user_id=user.id,action="user_created",entity_type="user",entity_id=str(target.id),after_json={"role":data.role,"scopes":data.scopes})); db.commit(); return {"id": target.id, "login": target.login}


@app.patch("/api/users/{user_id:int}")
def update_user(user_id: int, data: UserIn, user: User = Depends(current_user), db: Session = Depends(mutation_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可管理用户")
    target = db.get(User, user_id)
    if not target: raise HTTPException(404, "用户不存在")
    validate_user_scopes(db,data)
    if target.id==user.id and (not data.active or data.role!="admin"): raise HTTPException(409,"不能停用当前管理员或取消自己的管理员权限")
    target.display_name = data.display_name; target.role = data.role; target.active = data.active
    if data.password: target.password_hash = hash_password(data.password)
    for scope in db.scalars(select(UserCompanyScope).where(UserCompanyScope.user_id == user_id)): db.delete(scope)
    db.flush()
    for scope in data.scopes: db.add(UserCompanyScope(user_id=user_id, company_id=int(scope["company_id"]), permissions=",".join(scope.get("permissions", ["view"]))))
    db.add(AuditLog(user_id=user.id,action="user_scopes_updated",entity_type="user",entity_id=str(user_id),after_json={"active":data.active,"role":data.role,"scopes":data.scopes})); db.commit(); return {"ok": True}


@app.get("/api/rules")
def rules(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可管理规则")
    return [{"id": x.id, "version": x.version, "company_id": x.company_id, "conditions_json": x.conditions_json, "result_class": x.result_class, "active": x.active} for x in db.scalars(select(ClassificationRule).order_by(ClassificationRule.id))]


@app.post("/api/rules")
def create_rule(data: RuleIn, user: User = Depends(current_user), db: Session = Depends(mutation_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可管理规则")
    validate_rule(data)
    item = ClassificationRule(version=data.version, company_id=data.company_id, conditions_json=data.conditions_json, result_class=data.result_class, active=data.active); db.add(item); db.flush()
    db.add(AuditLog(user_id=user.id,action="rule_created",entity_type="rule",entity_id=str(item.id),after_json=data.model_dump())); db.commit(); return {"id": item.id}


@app.patch("/api/rules/{rule_id:int}")
def update_rule(rule_id: int, data: RuleIn, user: User = Depends(current_user), db: Session = Depends(mutation_db)):
    if user.role != "admin": raise HTTPException(403, "只有管理员可管理规则")
    item = db.get(ClassificationRule, rule_id)
    if not item: raise HTTPException(404, "规则不存在")
    validate_rule(data)
    old={"version":item.version,"conditions_json":item.conditions_json,"active":item.active}
    item.version = data.version; item.company_id = data.company_id; item.conditions_json = data.conditions_json; item.result_class = data.result_class; item.active = data.active
    db.add(AuditLog(user_id=user.id,action="rule_updated",entity_type="rule",entity_id=str(rule_id),before_json=old,after_json=data.model_dump())); db.commit(); return {"ok": True}


def validate_rule(data):
    cond=data.conditions_json
    if not {"cfops","tp_nf","fin_nfe","role"}<=set(cond) or not isinstance(cond.get("cfops"),list) or not cond["cfops"]:
        raise HTTPException(422,"规则须明确 CFOP、tpNF、finNFe 和公司角色")
    if data.result_class not in CLASSES|{"import","transfer","gift"}: raise HTTPException(422,"规则业务分类无效")
    if data.result_class=="sale" and cond["role"]!="issuer" and cond["role"]!=["issuer"]: raise HTTPException(422,"销售规则必须限定本公司为发行方")


class SavedFilterIn(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    filters:dict=Field(default_factory=dict)


@app.get("/api/saved-filters")
def saved_filters(user:User=Depends(current_user),db:Session=Depends(get_db)):
    return [{"id":s.id,"name":s.name,"filters":s.filters} for s in db.scalars(select(SavedFilter).where(SavedFilter.user_id==user.id).order_by(SavedFilter.id.desc()))]


@app.post("/api/saved-filters")
def save_filter(data:SavedFilterIn,user:User=Depends(current_user),db:Session=Depends(mutation_db)):
    scoped_query(db,user,data.filters)
    item=SavedFilter(user_id=user.id,name=data.name,filters=data.filters);db.add(item);db.commit();return {"id":item.id}


@app.delete("/api/saved-filters/{filter_id:int}")
def delete_filter(filter_id:int,user:User=Depends(current_user),db:Session=Depends(mutation_db)):
    item=db.get(SavedFilter,filter_id)
    if not item or item.user_id!=user.id: raise HTTPException(404,"筛选不存在")
    db.delete(item);db.commit();return {"ok":True}


def validate_user_scopes(db,data):
    if data.role not in {"admin","finance","accountant","readonly"}: raise HTTPException(422,"用户角色无效")
    if data.password and len(data.password)<10: raise HTTPException(422,"密码至少10位")
    seen=set()
    for scope in data.scopes:
        cid=int(scope.get("company_id",0))
        if cid in seen or not db.get(Company,cid): raise HTTPException(422,"公司范围重复或不存在")
        seen.add(cid)
        permissions=scope.get("permissions",["view"])
        if not isinstance(permissions,list) or set(permissions)-{"view","import","export","classify"}: raise HTTPException(422,"公司权限无效")
