"""Idempotent, auditable ingestion for NF-e XML and ZIP uploads."""
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, BadZipFile
import io, os, stat, tempfile
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..config import settings
from ..models import Company, FileObject, Invoice, InvoiceItem, InvoiceFileVersion, InvoiceCompanyRole, InvoiceEvent, EventFileVersion, InvoiceTax, InvoiceReference, ClassificationRule, ImportBatch, ImportEntry, AuditLog
from .xml_parser import ParsedEvent, parse_xml, parse_date, normalize_cnpj, dec

_SALE_CFOPS = {"5101", "5102", "6101", "6102"}
_SALES_RETURN_CFOPS = {"1201", "1202", "2201", "2202"}
_PURCHASE_RETURN_CFOPS = {"5201", "5202", "6201", "6202"}

def save_object(db: Session, raw: bytes, original_name: str) -> FileObject:
    digest = sha256(raw).hexdigest(); obj = db.scalar(select(FileObject).where(FileObject.sha256 == digest))
    if obj:
        existing_path = settings.objects_dir / obj.relative_path
        if not existing_path.is_file() or sha256(existing_path.read_bytes()).hexdigest() != digest: raise IOError("已保存原文缺失或哈希不一致，请停止导入并恢复原文")
        return obj
    rel = f"{digest[:2]}/{digest}.xml"; path = settings.objects_dir / rel; path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if sha256(path.read_bytes()).hexdigest() != digest: raise IOError("原文对象哈希冲突")
    else:
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as fh:
                tmp = Path(fh.name); fh.write(raw); fh.flush(); os.fsync(fh.fileno())
            if sha256(tmp.read_bytes()).hexdigest() != digest: raise IOError("原文写入哈希校验失败")
            os.replace(tmp, path)
            if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(directory_fd)
                finally: os.close(directory_fd)
        finally:
            if tmp is not None: tmp.unlink(missing_ok=True)
    obj = FileObject(sha256=digest, size_bytes=len(raw), relative_path=rel, original_name=original_name); db.add(obj); db.flush(); return obj

def company_for(db: Session, cnpj: str, target: Company | None, name: str = "") -> Company | None:
    cnpj = normalize_cnpj(cnpj)
    if not cnpj: return target
    company = db.scalar(select(Company).where(Company.cnpj == cnpj))
    if company: return company
    company = Company(cnpj=cnpj, legal_name=name or "待确认公司", alias="", confirmed=False); db.add(company); db.flush(); return company

def role_for(db: Session, invoice: Invoice, company: Company, role: str, business_class: str, included: bool, internal_trade: bool = False, rule_version="builtin-2.0"):
    existing = db.scalar(select(InvoiceCompanyRole).where(InvoiceCompanyRole.invoice_id == invoice.id, InvoiceCompanyRole.company_id == company.id, InvoiceCompanyRole.role == role))
    if existing:
        # Confirmed/manual classifications are authoritative until a human changes them.
        if existing.review_state != "confirmed":
            existing.business_class, existing.included, existing.internal_trade = business_class, included, internal_trade
            existing.rule_version = rule_version
            if included: existing.review_state = "confirmed"
        else:
            existing.internal_trade = internal_trade
        return existing
    item = InvoiceCompanyRole(invoice_id=invoice.id, company_id=company.id, role=role, business_class=business_class, review_state="confirmed" if included else "pending", rule_version=rule_version, included=included, internal_trade=internal_trade); db.add(item); return item

def classification_decision(parsed, company: Company | None, invoice: Invoice, db=None):
    """Conservative rules independent from fiscal status and company review."""
    if not company or not company.confirmed: return "pending", False, "builtin-2.0"
    eligible = invoice.fiscal_status == "authorized" and not invoice.conflict and company.active is not False
    item_cfops = {str(i.cfop).strip() for i in parsed.items if i.cfop}
    if not parsed.items or len([i for i in parsed.items if i.cfop]) != len(parsed.items): return "pending", False, "builtin-2.0"
    company_role = "issuer" if parsed.issuer_cnpj == company.cnpj else "recipient" if parsed.recipient_cnpj == company.cnpj else "other"
    if db is not None:
        rules = db.scalars(select(ClassificationRule).where(ClassificationRule.active.is_(True), (ClassificationRule.company_id == company.id) | ClassificationRule.company_id.is_(None)).order_by(ClassificationRule.company_id.desc(), ClassificationRule.id.desc())).all()
        for rule in rules:
            cond = rule.conditions_json or {}
            # Every rule must explicitly constrain all CFOPs, issuer role,
            # direction and purpose; a broad tpNF-only rule is never sufficient.
            required = {"cfops", "tp_nf", "fin_nfe", "role"}
            if not required <= cond.keys() or set(cond) - required - {"model"}: continue
            expected_cfops = set(map(str, cond.get("cfops", [])))
            def matches(name, value):
                expected = cond.get(name)
                return value in list(map(str, expected)) if isinstance(expected, list) else value == str(expected)
            if item_cfops and item_cfops <= expected_cfops and matches("tp_nf", parsed.tp_nf) and matches("fin_nfe", parsed.fin_nfe) and matches("role", company_role) and ("model" not in cond or matches("model", parsed.model)):
                cls = rule.result_class
                if cls in {"sale", "sales_return", "purchase", "purchase_return", "import", "transfer", "gift", "other", "pending"}:
                    return cls, eligible and cls in {"sale", "sales_return"}, rule.version
    if parsed.issuer_cnpj == company.cnpj:
        if parsed.tp_nf == "0" and parsed.fin_nfe == "4" and item_cfops <= _SALES_RETURN_CFOPS: return "sales_return", eligible, "builtin-2.0"
        if parsed.tp_nf == "1" and parsed.fin_nfe == "1" and item_cfops <= _SALE_CFOPS: return "sale", eligible, "builtin-2.0"
        if parsed.tp_nf == "1" and parsed.fin_nfe == "4" and item_cfops <= _PURCHASE_RETURN_CFOPS: return "purchase_return", False, "builtin-2.0"
        # Unsupported/mixed directions, transfers, consignment and gifts need
        # explicit company rules or human review; no tpNF blanket sales rule.
        if parsed.tp_nf == "0" and all(x.startswith("3") for x in item_cfops): return "import", False, "builtin-2.0"
        if parsed.tp_nf == "0" and parsed.fin_nfe == "1" and all(x.startswith(("1", "2")) for x in item_cfops): return "purchase", False, "builtin-2.0"
        return "pending", False, "builtin-2.0"
    if parsed.recipient_cnpj == company.cnpj:
        # Customer issues an outgoing return of purchased goods back to our
        # company. Relative to our company this is a sales return.
        if parsed.tp_nf == "1" and parsed.fin_nfe == "4" and item_cfops <= _PURCHASE_RETURN_CFOPS: return "sales_return", eligible, "builtin-2.0"
        if parsed.tp_nf == "1" and parsed.fin_nfe == "1": return "purchase", False, "builtin-2.0"
        return "pending", False, "builtin-2.0"
    return "other", False, "builtin-2.0"

def classify(parsed, company: Company | None, invoice: Invoice):
    cls, included, _ = classification_decision(parsed, company, invoice)
    return cls, included

def _invoice_fingerprint(invoice, items, db=None):
    from .xml_parser import ParsedDocument, ParsedItem
    if (invoice.extra_json or {}).get("business_fingerprint"): return invoice.extra_json["business_fingerprint"]
    # Legacy records are compared against their immutable primary XML, not
    # against fields omitted by parser v1, avoiding false conflicts on reupload.
    if db is not None:
        version = db.scalar(select(InvoiceFileVersion).where(InvoiceFileVersion.invoice_id == invoice.id).order_by(InvoiceFileVersion.is_primary.desc(), InvoiceFileVersion.id))
        obj = db.get(FileObject, version.object_id) if version else None
        if obj:
            raw = (settings.objects_dir / obj.relative_path).read_bytes()
            if sha256(raw).hexdigest() != obj.sha256: raise IOError("旧版原文哈希校验失败")
            return parse_xml(raw).business_fingerprint()
    p = ParsedDocument("nfe", invoice.access_key, invoice.environment, invoice.model, invoice.issuer_cnpj, invoice.recipient_cnpj, "", invoice.number, invoice.series, parse_date(invoice.issue_text) or invoice.issue_at, invoice.issue_text, invoice.tp_nf, invoice.fin_nfe, invoice.fiscal_status, Decimal(invoice.v_nf or 0), Decimal(invoice.v_prod or 0), Decimal(invoice.v_desc or 0), [ParsedItem(i.line_no, i.c_prod, i.x_prod, i.ncm, i.cfop, i.u_com, Decimal(i.q_com or 0), Decimal(i.v_un_com or 0), Decimal(i.v_prod or 0), Decimal(i.v_desc or 0), (i.extra_json or {}).get("raw_taxes", [])) for i in items], [], (invoice.extra_json or {}))
    return p.business_fingerprint()

def _upsert_event(db, parsed_event: ParsedEvent, invoice: Invoice | None = None):
    e = db.scalar(select(InvoiceEvent).where(InvoiceEvent.environment == parsed_event.environment, InvoiceEvent.access_key == parsed_event.access_key, InvoiceEvent.event_type == parsed_event.event_type, InvoiceEvent.sequence == parsed_event.sequence))
    if not e:
        e = InvoiceEvent(invoice_id=invoice.id if invoice else None, environment=parsed_event.environment, access_key=parsed_event.access_key, event_type=parsed_event.event_type, sequence=parsed_event.sequence, accepted=parsed_event.accepted, receipt_status=parsed_event.receipt_status, protocol=parsed_event.protocol, event_at=parsed_event.event_at, raw_json=parsed_event.raw); db.add(e); db.flush()
    else:
        if parsed_event.accepted or not e.accepted:
            e.receipt_status = parsed_event.receipt_status or e.receipt_status; e.protocol = parsed_event.protocol or e.protocol
        e.accepted = e.accepted or parsed_event.accepted
        e.raw_json = {**(e.raw_json or {}), **(parsed_event.raw or {}), "request": bool((e.raw_json or {}).get("request") or parsed_event.request_present), "receipt": bool((e.raw_json or {}).get("receipt") or parsed_event.receipt_present)}
        if invoice and e.invoice_id is None: e.invoice_id = invoice.id
    return e

def _record_event_version(db, event, obj, kind="event"):
    if not db.scalar(select(EventFileVersion).where(EventFileVersion.event_id == event.id, EventFileVersion.object_id == obj.id)):
        db.add(EventFileVersion(event_id=event.id, object_id=obj.id, kind=kind))

def _apply_events(db, invoice: Invoice, events: list[ParsedEvent]):
    changed = False; result = None
    for ev in events:
        if ev.environment != invoice.environment or ev.access_key != invoice.access_key: continue
        event = _upsert_event(db, ev, invoice)
        # 136 (event registered, no accepted cancellation) is deliberately not accepted.
        credible_cancel = ev.event_type == "110111" and event.accepted and (not ev.issuer_cnpj or ev.issuer_cnpj == invoice.issuer_cnpj)
        if credible_cancel and invoice.fiscal_status != "canceled":
            invoice.fiscal_status = "canceled"; changed = True
            for role in db.scalars(select(InvoiceCompanyRole).where(InvoiceCompanyRole.invoice_id == invoice.id)):
                role.included = False
            db.add(AuditLog(action="cancel_evidence_merged", entity_type="invoice", entity_id=str(invoice.id), before_json={"fiscal_status": "authorized"}, after_json={"fiscal_status": "canceled", "event_id": event.id}))
            result = event
    return result if changed else None

def process_xml(db: Session, raw: bytes, name: str, batch: ImportBatch, target: Company | None, allowed_cnpjs: set[str] | None = None):
    # PostgreSQL advisory locks serialize identical bytes and identities across
    # independent workers. Unique constraints remain the final protection.
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy import text as sql_text
        lock_id = int.from_bytes(sha256(raw).digest()[:8], "big", signed=True)
        db.execute(sql_text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})
    obj = save_object(db, raw, name)
    try: parsed = parse_xml(raw)
    except Exception as exc:
        db.add(ImportEntry(batch_id=batch.id, member_path=name, object_id=obj.id, result="failed", error_code=str(exc)[:100])); db.flush(); return "failed", None, None, str(exc)
    if parsed.kind == "unsupported":
        db.add(ImportEntry(batch_id=batch.id, member_path=name, object_id=obj.id, result="unsupported", error_code=parsed.raw_metadata.get("reason"))); db.flush(); return "unsupported", None, None, parsed.raw_metadata.get("reason")
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy import text as sql_text
        identities = {f"{ev.environment}:{ev.access_key}" for ev in parsed.events}
        if parsed.access_key: identities.add(f"{parsed.environment}:{parsed.access_key}")
        for identity in sorted(identities):
            lock_id = int.from_bytes(sha256(identity.encode()).digest()[:8], "big", signed=True)
            db.execute(sql_text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})
    if parsed.kind == "event":
        if allowed_cnpjs is not None and not all(ev.issuer_cnpj in {normalize_cnpj(x) for x in allowed_cnpjs} for ev in parsed.events):
            msg = "事件发行方不在授权公司范围"; db.add(ImportEntry(batch_id=batch.id, member_path=name, object_id=obj.id, result="failed", error_code=msg)); db.flush(); return "failed", None, None, msg
        if target and not all(ev.issuer_cnpj == target.cnpj for ev in parsed.events):
            msg = "事件发行方与目标公司不匹配"; db.add(ImportEntry(batch_id=batch.id, member_path=name, object_id=obj.id, result="failed", error_code=msg)); db.flush(); return "failed", None, None, msg
        event = None
        for ev in parsed.events:
            # Events may arrive first or target multiple invoices in one file.
            inv = db.scalar(select(Invoice).where(Invoice.environment == ev.environment, Invoice.access_key == ev.access_key))
            if inv and ev.issuer_cnpj and ev.issuer_cnpj != inv.issuer_cnpj:
                msg = "取消事件发行方与发票不匹配"; db.add(ImportEntry(batch_id=batch.id, member_path=name, object_id=obj.id, result="failed", error_code=msg)); db.flush(); return "failed", None, None, msg
            event = _upsert_event(db, ev, inv); _record_event_version(db, event, obj, "receipt" if ev.receipt_present and not ev.request_present else "processed_event" if ev.receipt_present else "request")
            if inv: _apply_events(db, inv, [ev])
        db.add(ImportEntry(batch_id=batch.id, member_path=name, object_id=obj.id, result="event", event_id=event.id if event else None)); db.flush(); return "event", None, event, None
    if allowed_cnpjs is not None:
        allowed = {normalize_cnpj(x) for x in allowed_cnpjs}
        if parsed.issuer_cnpj not in allowed and parsed.recipient_cnpj not in allowed:
            msg = "发票 emit/dest 不在授权公司范围"; db.add(ImportEntry(batch_id=batch.id, member_path=name, object_id=obj.id, result="failed", error_code=msg)); db.flush(); return "failed", None, None, msg
    if target and target.cnpj not in {parsed.issuer_cnpj, parsed.recipient_cnpj}:
        msg = "发票 emit/dest 与目标公司不匹配"; db.add(ImportEntry(batch_id=batch.id, member_path=name, object_id=obj.id, result="failed", error_code=msg)); db.flush(); return "failed", None, None, msg
    invoice = db.scalar(select(Invoice).where(Invoice.environment == parsed.environment, Invoice.access_key == parsed.access_key)); result = "new"; is_new = invoice is None
    if invoice:
        result = "duplicate"; existing_items = db.scalars(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)).all()
        if _invoice_fingerprint(invoice, existing_items, db) != parsed.business_fingerprint():
            previous_status = invoice.fiscal_status; invoice.conflict = True; invoice.fiscal_status = "conflict"; result = "conflict"
            for role in db.scalars(select(InvoiceCompanyRole).where(InvoiceCompanyRole.invoice_id == invoice.id)): role.included = False
            db.add(AuditLog(user_id=batch.created_by, action="invoice_conflict_quarantined", entity_type="invoice", entity_id=str(invoice.id), before_json={"fiscal_status": previous_status}, after_json={"fiscal_status": "conflict", "source_sha256": obj.sha256}))
        elif parsed.fiscal_status == "authorized" and invoice.fiscal_status == "pending_evidence":
            invoice.fiscal_status = "authorized"; invoice.extra_json = {**(invoice.extra_json or {}), **parsed.raw_metadata}; result = "updated"
    else:
        invoice = Invoice(environment=parsed.environment, access_key=parsed.access_key, model=parsed.model, issuer_cnpj=parsed.issuer_cnpj, recipient_cnpj=parsed.recipient_cnpj, number=parsed.number, series=parsed.series, issue_at=parsed.issue_at, issue_text=parsed.issue_text, issue_local_date=parsed.business_date, tp_nf=parsed.tp_nf, fin_nfe=parsed.fin_nfe, fiscal_status=parsed.fiscal_status, v_nf=parsed.v_nf, v_prod=parsed.v_prod, v_desc=parsed.v_desc, extra_json=parsed.raw_metadata); db.add(invoice); db.flush()
        for item in parsed.items: db.add(InvoiceItem(invoice_id=invoice.id, line_no=item.line_no, c_prod=item.c_prod, x_prod=item.x_prod, ncm=item.ncm, cfop=item.cfop, u_com=item.u_com, q_com=item.q_com, v_un_com=item.v_un_com, v_prod=item.v_prod, v_desc=item.v_desc, extra_json={"raw_taxes": item.raw_taxes}))
        db.flush()
        rows = {i.line_no: i for i in db.scalars(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id))}
        for source in parsed.items:
            row = rows[source.line_no]
            for tax in source.raw_taxes:
                vals = tax.get("values", {}); typ = tax.get("type", "")
                amount_field = {"ICMS": "vICMS", "IPI": "vIPI", "PIS": "vPIS", "COFINS": "vCOFINS", "II": "vII"}.get(typ)
                rate_field = {"ICMS": "pICMS", "IPI": "pIPI", "PIS": "pPIS", "COFINS": "pCOFINS"}.get(typ)
                db.add(InvoiceTax(invoice_id=invoice.id, item_id=row.id, scope="item", tax_type=typ, cst_csosn=vals.get("CST") or vals.get("CSOSN") or "", base=dec(vals["vBC"]) if vals.get("vBC") else None, rate=dec(vals[rate_field]) if rate_field and vals.get(rate_field) else None, amount=dec(vals[amount_field]) if amount_field and vals.get(amount_field) else None, raw_json=tax))
        if parsed.raw_metadata.get("header_taxes"):
            db.add(InvoiceTax(invoice_id=invoice.id, item_id=None, scope="header", tax_type="ICMSTot", cst_csosn="", base=None, rate=None, amount=None, raw_json=parsed.raw_metadata["header_taxes"]))
        for ref in parsed.raw_metadata.get("references", []): db.add(InvoiceReference(invoice_id=invoice.id, referenced_key=ref, relationship_type="NFref"))
    prior = db.scalars(select(InvoiceFileVersion).where(InvoiceFileVersion.invoice_id == invoice.id)).all()
    if not db.scalar(select(InvoiceFileVersion).where(InvoiceFileVersion.invoice_id == invoice.id, InvoiceFileVersion.object_id == obj.id)):
        promote = result != "conflict" and parsed.fiscal_status == "authorized" and not any(v.is_primary and v.evidence_level == "authorized" for v in prior)
        if promote:
            for version in prior: version.is_primary = False
        db.add(InvoiceFileVersion(invoice_id=invoice.id, object_id=obj.id, document_kind=parsed.kind, evidence_level="authorized" if parsed.fiscal_status == "authorized" else "raw", is_primary=(not prior) or promote))
    if result == "conflict":
        # Keep original parties/items/primary version and freeze contribution.
        # A conflicting source must not create new company roles or cancel it.
        db.add(ImportEntry(batch_id=batch.id, member_path=name, object_id=obj.id, result=result, invoice_id=invoice.id)); db.flush(); return result, invoice, None, None
    issuer_company = db.scalar(select(Company).where(Company.cnpj == parsed.issuer_cnpj))
    recipient_company = db.scalar(select(Company).where(Company.cnpj == parsed.recipient_cnpj)) if parsed.recipient_cnpj else None
    # Buyers are not automatically group companies. Only unknown emitters may
    # become candidates, and a scoped user must never create out-of-scope firms.
    if issuer_company is None and target is None and (allowed_cnpjs is None or parsed.issuer_cnpj in allowed_cnpjs):
        issuer_company = company_for(db, parsed.issuer_cnpj, None, parsed.issuer_name)
    internal = bool(issuer_company and issuer_company.confirmed and recipient_company and recipient_company.confirmed and issuer_company.id != recipient_company.id)
    related = [(issuer_company, "issuer"), (recipient_company, "recipient")]
    for company, company_role in related:
        if company:
            cls, included, rule_version = classification_decision(parsed, company, invoice, db)
            role_for(db, invoice, company, company_role, cls, included and not invoice.conflict, internal, rule_version)
    pending = db.scalars(select(InvoiceEvent).where(InvoiceEvent.environment == parsed.environment, InvoiceEvent.access_key == parsed.access_key, InvoiceEvent.invoice_id.is_(None))).all()
    events = parsed.events + [ParsedEvent(e.access_key, e.event_type, e.sequence, e.accepted, e.receipt_status, e.protocol, e.event_at, e.environment, (e.raw_json or {}).get("issuer_cnpj") or e.access_key[6:20], e.raw_json) for e in pending]
    for ev in parsed.events:
        if ev.environment == invoice.environment and ev.access_key == invoice.access_key:
            _record_event_version(db, _upsert_event(db, ev, invoice), obj, "embedded_event")
    event = _apply_events(db, invoice, events)
    db.add(ImportEntry(batch_id=batch.id, member_path=name, object_id=obj.id, result=result, invoice_id=invoice.id)); db.flush(); return result, invoice, event, None

def iter_upload(raw: bytes, filename: str):
    if filename.lower().endswith(".zip") or raw[:2] == b"PK":
        try:
            with ZipFile(io.BytesIO(raw)) as z:
                infos = z.infolist(); max_members = getattr(settings, "max_zip_members", 50000); max_total = getattr(settings, "max_zip_expanded_bytes", 5 * 1024 * 1024 * 1024); max_member = getattr(settings, "max_xml_bytes", 10 * 1024 * 1024)
                if len(infos) > max_members: raise ValueError("ZIP 成员超过配置上限")
                total = 0
                for info in infos:
                    if info.is_dir() or info.filename.endswith("/"): continue
                    p = PurePosixPath(info.filename.replace("\\", "/"))
                    if p.is_absolute() or any(part in ("", ".", "..") for part in p.parts) or "\x00" in info.filename or any(":" in part for part in p.parts): raise ValueError("ZIP 包含目录穿越路径")
                    if stat.S_ISLNK((info.external_attr >> 16) & 0o170000): raise ValueError("ZIP 包含符号链接")
                    if info.flag_bits & 0x1: raise ValueError("不支持加密 ZIP")
                    if p.suffix.lower() == ".zip": raise ValueError("不支持嵌套 ZIP")
                    if info.file_size > max_member: raise ValueError("ZIP 成员超过 XML 大小上限")
                    if info.compress_size and info.file_size / info.compress_size > 200: raise ValueError("ZIP 压缩比超过安全上限")
                    total += info.file_size
                    if total > max_total: raise ValueError("ZIP 展开总量超过配置上限")
                    data = z.read(info)
                    if len(data) != info.file_size: raise ValueError("ZIP 成员读取大小不一致")
                    yield info.filename, data
        except BadZipFile as exc: raise ValueError("ZIP 文件损坏") from exc
    else:
        if len(raw) > getattr(settings, "max_xml_bytes", 10 * 1024 * 1024): raise ValueError("XML 超过大小上限")
        yield filename, raw
