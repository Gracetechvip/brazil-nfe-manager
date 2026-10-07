from sqlalchemy import select
from fastapi import HTTPException
from ..models import UserCompanyScope, InvoiceCompanyRole, Company


def company_ids(db, user, permission="view"):
    if user.role == "admin":
        return None
    scopes = db.scalars(select(UserCompanyScope).where(UserCompanyScope.user_id == user.id)).all()
    return {s.company_id for s in scopes if permission in s.permissions.split(",")}


def permits(db, user, company_id, permission="view"):
    ids = company_ids(db, user, permission)
    return ids is None or company_id in ids


def invoice_companies(db, user, invoice_id, permission="view"):
    ids = company_ids(db, user, permission)
    roles = db.scalars(select(InvoiceCompanyRole).where(InvoiceCompanyRole.invoice_id == invoice_id)).all()
    return sorted({r.company_id for r in roles if ids is None or r.company_id in ids})


def require_invoice(db, user, invoice_id, permission="view"):
    ids = invoice_companies(db, user, invoice_id, permission)
    if not ids:
        raise HTTPException(403, "没有该公司范围的操作权限")
    return ids
