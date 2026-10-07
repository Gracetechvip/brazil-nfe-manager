from datetime import date
from decimal import Decimal, InvalidOperation
from sqlalchemy import select
from fastapi import HTTPException
from ..models import Invoice, InvoiceCompanyRole, InvoiceItem
from .access import company_ids, permits

CLASSES = {"sale", "sales_return", "purchase", "purchase_return", "other", "pending", "internal", "import", "transfer", "gift"}


def item_conditions(params):
    clauses = []
    if params.get("sku"):
        if str(params.get("sku_exact", "false")).lower() in {"true", "1"}:
            clauses.append(InvoiceItem.c_prod == params["sku"])
        else:
            clauses.append(InvoiceItem.c_prod.ilike("%" + params["sku"] + "%"))
    if params.get("product_name"):
        clauses.append(InvoiceItem.x_prod.ilike("%" + params["product_name"] + "%"))
    for key, attr in (("ncm", InvoiceItem.ncm), ("cfop", InvoiceItem.cfop)):
        if params.get(key): clauses.append(attr == params[key])
    return clauses


def invoice_query(db, user, params, permission="view"):
    params = {k: v for k, v in params.items() if v is not None and v != ""}
    try:
        start = date.fromisoformat(params["date_from"]) if params.get("date_from") else None
        end = date.fromisoformat(params["date_to"]) if params.get("date_to") else None
        lo = Decimal(str(params["min_amount"])) if params.get("min_amount") is not None else None
        hi = Decimal(str(params["max_amount"])) if params.get("max_amount") is not None else None
        if start and end and start > end: raise ValueError("开始日期不能晚于结束日期")
        if any(x is not None and not x.is_finite() for x in (lo, hi)): raise ValueError("金额须为有限十进制数")
        if lo is not None and hi is not None and lo > hi: raise ValueError("最小金额不能大于最大金额")
        cid = int(params["company_id"]) if params.get("company_id") else None
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise HTTPException(422, str(exc) or "筛选条件无效") from exc
    ids = company_ids(db, user, permission)
    role_conditions = [InvoiceCompanyRole.invoice_id == Invoice.id]
    if ids is not None: role_conditions.append(InvoiceCompanyRole.company_id.in_(ids))
    if cid:
        if not permits(db, user, cid, permission): raise HTTPException(403, "无该公司的操作权限")
        role_conditions.append(InvoiceCompanyRole.company_id == cid)
    if params.get("business_class"):
        if params["business_class"] not in CLASSES: raise HTTPException(422, "业务分类无效")
        role_conditions.append(InvoiceCompanyRole.business_class == params["business_class"])
    environment = params.get("environment", "production")
    if environment not in {"production", "test", "all"}: raise HTTPException(422, "环境无效")
    q = select(Invoice).where(select(InvoiceCompanyRole.id).where(*role_conditions).exists())
    if environment != "all": q = q.where(Invoice.environment == environment)
    if start: q = q.where(Invoice.issue_local_date >= start)
    if end: q = q.where(Invoice.issue_local_date <= end)
    if lo is not None: q = q.where(Invoice.v_nf >= lo)
    if hi is not None: q = q.where(Invoice.v_nf <= hi)
    if params.get("status"): q = q.where(Invoice.fiscal_status == params["status"])
    if params.get("search"):
        term = "%" + str(params["search"]) + "%"
        from sqlalchemy import or_
        q = q.where(or_(Invoice.access_key.ilike(term), Invoice.number.ilike(term)))
    clauses = item_conditions(params)
    if clauses:
        q = q.where(select(InvoiceItem.id).where(InvoiceItem.invoice_id == Invoice.id, *clauses).exists())
    return q.order_by(Invoice.issue_local_date.desc(), Invoice.id.desc())
