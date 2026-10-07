from decimal import Decimal
from sqlalchemy import select
from ..models import Company, InvoiceCompanyRole, CoveragePeriod
from .access import company_ids
from .query import invoice_query


def summarize(db, user, params):
    ids = company_ids(db, user)
    selected = int(params["company_id"]) if params.get("company_id") else None
    include_internal = str(params.get("include_internal", "false")).lower() in {"true", "1"}
    totals = {"sales_amount": Decimal(0), "returns_amount": Decimal(0), "pending_amount": Decimal(0), "canceled_amount": Decimal(0), "internal_amount": Decimal(0)}
    counts = {"invoice_count": 0, "pending_count": 0}
    trends, ranking = {}, {}
    companies = {c.id: c for c in db.scalars(select(Company))}
    first = None
    for i in db.scalars(invoice_query(db, user, params)):
        if i.issue_local_date: first = min(first, i.issue_local_date) if first else i.issue_local_date
        roles = [r for r in db.scalars(select(InvoiceCompanyRole).where(InvoiceCompanyRole.invoice_id == i.id)) if (ids is None or r.company_id in ids) and (selected is None or selected == r.company_id)]
        seen_pending = False
        for r in roles:
            c = companies[r.company_id]
            amount = Decimal(i.v_nf)
            if not c.confirmed or r.business_class == "pending" or i.conflict or i.fiscal_status in {"pending_evidence", "conflict"}:
                if not seen_pending:
                    totals["pending_amount"] += amount; counts["pending_count"] += 1; seen_pending = True
                continue
            if i.environment != "production" or not c.active: continue
            if r.business_class == "sale" and i.fiscal_status == "canceled": totals["canceled_amount"] += amount
            if r.internal_trade:
                if r.role == "issuer": totals["internal_amount"] += amount
                if not include_internal and selected is None: continue
            if i.fiscal_status != "authorized" or not r.included: continue
            if r.business_class == "sale" and r.role == "issuer":
                totals["sales_amount"] += amount; counts["invoice_count"] += 1; value = amount
            elif r.business_class == "sales_return":
                totals["returns_amount"] += amount; value = -amount
            else: continue
            if i.issue_local_date:
                d = i.issue_local_date
                granularity = params.get("granularity", "month")
                period = d.isoformat() if granularity == "day" else f"{d.isocalendar().year}-W{d.isocalendar().week:02d}" if granularity == "week" else d.strftime("%Y") if granularity == "year" else d.strftime("%Y-%m")
                trends[period] = trends.get(period, Decimal(0)) + value
            ranking[r.company_id] = ranking.get(r.company_id, Decimal(0)) + value
    totals["net_amount"] = totals["sales_amount"] - totals["returns_amount"]
    summary = {k: format(v, ".2f") for k,v in totals.items()} | counts
    summary |= {"coverage_note": "仅统计已导入资料；缺失月份标记未导入，不能视为零销售。", "history_start": first.isoformat() if first else None}
    ranks = [{"company_id": cid, "cnpj": companies[cid].cnpj, "legal_name": companies[cid].legal_name, "amount": format(amount, ".2f")} for cid,amount in sorted(ranking.items(), key=lambda x:x[1], reverse=True)]
    return summary, [{"period": k, "amount": format(v, ".2f")} for k,v in sorted(trends.items())], ranks
