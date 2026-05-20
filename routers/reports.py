from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Response, status

from core.dependencies import get_current_user, require_admin
from core.firebase import Collections, get_db
from services.report_service import (
    build_sales_report,
    build_transaction_report,
    export_report_to_csv,
    export_report_to_excel,
    export_report_to_pdf,
)

router = APIRouter()


def _date_range(period: str) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "weekly":
        start = now - timedelta(days=7)
    elif period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(days=30)
    return start, now


@router.get("/transactions", dependencies=[Depends(require_admin)])
async def transaction_report(
    period: str = Query("daily", enum=["daily", "weekly", "monthly"]),
    station_id: str | None = Query(None),
    fuel_type: str | None = Query(None),
):
    db = get_db()
    start, end = _date_range(period)
    return build_transaction_report(db, start, end, station_id=station_id, fuel_type=fuel_type)


@router.get("/sales", dependencies=[Depends(require_admin)])
async def sales_report(
    period: str = Query("daily", enum=["daily", "weekly", "monthly"]),
    station_id: str | None = Query(None),
):
    db = get_db()
    start, end = _date_range(period)
    return build_sales_report(db, start, end, station_id=station_id)


@router.get("/fraud", dependencies=[Depends(require_admin)])
async def fraud_report(period: str = Query("monthly")):
    db = get_db()
    start, end = _date_range(period)
    docs = (
        db.collection(Collections.FRAUD_ALERTS)
        .where("created_at", ">=", start)
        .where("created_at", "<=", end)
        .get()
    )
    alerts  = [{"id": d.id, **d.to_dict()} for d in docs]
    by_type: dict[str, int] = {}
    for a in alerts:
        t = a.get("alert_type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
    return {"period": period, "total": len(alerts), "by_type": by_type, "items": alerts}


@router.get("/employees", dependencies=[Depends(require_admin)])
async def employee_report(period: str = Query("monthly")):
    db = get_db()
    start, end = _date_range(period)
    docs = (
        db.collection(Collections.TRANSACTIONS)
        .where("created_at", ">=", start)
        .where("created_at", "<=", end)
        .get()
    )
    perf: dict[str, dict] = {}
    for d in docs:
        tx     = d.to_dict()
        emp_id = tx.get("employee_id")
        if not emp_id:
            continue
        if emp_id not in perf:
            perf[emp_id] = {"transaction_count": 0, "total_revenue": 0.0, "total_litres": 0.0}
        perf[emp_id]["transaction_count"] += 1
        perf[emp_id]["total_revenue"]     += tx.get("total_amount", 0.0)
        perf[emp_id]["total_litres"]      += tx.get("litres_dispensed", 0.0)

    # Enrich with employee profile data
    emp_ids = list(perf.keys())
    for emp_id in emp_ids:
        emp_doc = db.collection(Collections.USERS).document(emp_id).get()
        if emp_doc.exists:
            emp_data = emp_doc.to_dict()
            perf[emp_id]["full_name"] = emp_data.get("full_name")
            perf[emp_id]["email"]     = emp_data.get("email")
            perf[emp_id]["phone"]     = emp_data.get("phone")

    return {"period": period, "employees": perf}


@router.get("/nozzles", dependencies=[Depends(require_admin)])
async def nozzle_report(
    period: str = Query("daily"),
    station_id: str | None = Query(None),
):
    db = get_db()
    start, end = _date_range(period)
    docs = (
        db.collection(Collections.TRANSACTIONS)
        .where("created_at", ">=", start)
        .where("created_at", "<=", end)
        .get()
    )
    perf: dict[str, dict] = {}
    for d in docs:
        tx = d.to_dict()
        if station_id and tx.get("station_id") != station_id:
            continue
        nid = tx.get("nozzle_id", "unknown")
        if nid not in perf:
            perf[nid] = {"transaction_count": 0, "total_revenue": 0.0, "total_litres": 0.0}
        perf[nid]["transaction_count"] += 1
        perf[nid]["total_revenue"]     += tx.get("total_amount", 0.0)
        perf[nid]["total_litres"]      += tx.get("litres_dispensed", 0.0)
    return {"period": period, "nozzles": perf}


@router.get("/comparative", dependencies=[Depends(require_admin)])
async def comparative_report(
    metric: str = Query("revenue", enum=["revenue", "litres", "transactions"]),
    current_period: str = Query("monthly"),
    previous_period: str = Query("monthly"),
):
    db = get_db()
    curr_start, curr_end = _date_range(current_period)

    if previous_period == "monthly":
        prev_end   = curr_start
        prev_start = (prev_end.replace(day=1) - timedelta(days=1)).replace(day=1)
    else:
        delta      = curr_end - curr_start
        prev_end   = curr_start
        prev_start = prev_end - delta

    field_map = {
        "revenue":      "total_amount",
        "litres":       "litres_dispensed",
        "transactions": None,  # count only — no numeric field to sum
    }
    field = field_map[metric]

    def _aggregate(start: datetime, end: datetime) -> tuple[float, int]:
        docs  = (
            db.collection(Collections.TRANSACTIONS)
            .where("created_at", ">=", start)
            .where("created_at", "<=", end)
            .get()
        )
        count = len(docs)
        total = count if field is None else sum(d.to_dict().get(field, 0) for d in docs)
        return float(total), count

    curr_total, curr_count = _aggregate(curr_start, curr_end)
    prev_total, prev_count = _aggregate(prev_start, prev_end)
    change_pct = round((curr_total - prev_total) / prev_total * 100, 2) if prev_total else 0.0

    return {
        "metric":         metric,
        "current":        {"total": curr_total, "count": curr_count},
        "previous":       {"total": prev_total, "count": prev_count},
        "change_percent": change_pct,
    }


@router.post("/export", dependencies=[Depends(require_admin)])
async def export_report(
    report_type: str = Query("transactions", enum=["transactions", "sales", "fraud", "employees"]),
    fmt: str = Query("pdf", alias="format", enum=["pdf", "excel", "csv"]),
    period: str = Query("monthly"),
):
    db = get_db()
    start, end = _date_range(period)

    if report_type == "transactions":
        data = build_transaction_report(db, start, end)
    elif report_type == "sales":
        data = build_sales_report(db, start, end)
    else:
        data = {"report_type": report_type, "period": period, "items": []}

    if fmt == "pdf":
        content    = export_report_to_pdf(data, report_type)
        media_type = "application/pdf"
        filename   = f"{report_type}_{period}.pdf"
    elif fmt == "excel":
        content    = export_report_to_excel(data, report_type)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename   = f"{report_type}_{period}.xlsx"
    else:
        content    = export_report_to_csv(data, report_type)
        media_type = "text/csv"
        filename   = f"{report_type}_{period}.csv"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/charts/transactions", dependencies=[Depends(require_admin)])
async def chart_transactions(period: str = Query("weekly")):
    db = get_db()
    start, end = _date_range(period)
    docs = (
        db.collection(Collections.TRANSACTIONS)
        .where("created_at", ">=", start)
        .where("created_at", "<=", end)
        .get()
    )
    daily: dict[str, dict] = {}
    for d in docs:
        tx      = d.to_dict()
        created = tx.get("created_at")
        if not created:
            continue
        day = created.strftime("%Y-%m-%d") if hasattr(created, "strftime") else str(created)[:10]
        if day not in daily:
            daily[day] = {"count": 0, "revenue": 0.0, "litres": 0.0}
        daily[day]["count"]   += 1
        daily[day]["revenue"] += tx.get("total_amount", 0.0)
        daily[day]["litres"]  += tx.get("litres_dispensed", 0.0)
    return {"period": period, "data": daily}


@router.get("/charts/revenue", dependencies=[Depends(require_admin)])
async def chart_revenue(period: str = Query("weekly")):
    db = get_db()
    start, end = _date_range(period)
    docs = (
        db.collection(Collections.TRANSACTIONS)
        .where("created_at", ">=", start)
        .where("created_at", "<=", end)
        .get()
    )
    daily: dict[str, float] = {}
    for d in docs:
        tx      = d.to_dict()
        created = tx.get("created_at")
        if not created:
            continue
        day        = created.strftime("%Y-%m-%d") if hasattr(created, "strftime") else str(created)[:10]
        daily[day] = daily.get(day, 0.0) + tx.get("total_amount", 0.0)
    return {"period": period, "revenue_by_day": daily}


@router.get("/charts/fraud", dependencies=[Depends(require_admin)])
async def chart_fraud(period: str = Query("monthly")):
    db = get_db()
    start, end = _date_range(period)
    docs = (
        db.collection(Collections.FRAUD_ALERTS)
        .where("created_at", ">=", start)
        .where("created_at", "<=", end)
        .get()
    )
    by_type: dict[str, int]     = {}
    by_severity: dict[str, int] = {}
    for d in docs:
        data = d.to_dict()
        t = data.get("alert_type", "unknown")
        s = data.get("severity", "unknown")
        by_type[t]     = by_type.get(t, 0) + 1
        by_severity[s] = by_severity.get(s, 0) + 1
    return {"period": period, "by_type": by_type, "by_severity": by_severity}


# ── User-level reporting (any authenticated user) ─────────────────────────────

def _filter_by_period(items: list[dict], period: str) -> list[dict]:
    """Filter items by period using Python (avoids composite Firestore index)."""
    if period == "all":
        return items
    start, end = _date_range(period)
    result = []
    for x in items:
        created = x.get("created_at")
        if created is None:
            continue
        if hasattr(created, "tzinfo") and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if start <= created <= end:
            result.append(x)
    return result


@router.get("/my-summary")
async def my_report_summary(
    period: str = Query("monthly", enum=["daily", "weekly", "monthly", "all"]),
    current_user: dict = Depends(get_current_user),
):
    """Aggregated stats for the current user's own transactions."""
    db = get_db()
    # Fetch all user transactions with a single-field query (no composite index needed).
    all_items = [{"id": d.id, **d.to_dict()} for d in
                 db.collection(Collections.TRANSACTIONS).where("user_id", "==", current_user["uid"]).get()]
    items = _filter_by_period(all_items, period)

    total_spent  = sum(x.get("total_amount", 0) for x in items)
    total_litres = sum(x.get("litres_dispensed", 0) for x in items)
    count        = len(items)

    fuel_breakdown:    dict[str, dict]  = {}
    payment_breakdown: dict[str, int]   = {}
    daily_spending:    dict[str, float] = {}

    for item in items:
        ft = item.get("fuel_type", "unknown")
        pm = item.get("payment_method", "cash")

        fuel_breakdown.setdefault(ft, {"amount": 0.0, "litres": 0.0, "count": 0})
        fuel_breakdown[ft]["amount"] += item.get("total_amount", 0)
        fuel_breakdown[ft]["litres"] += item.get("litres_dispensed", 0)
        fuel_breakdown[ft]["count"]  += 1

        payment_breakdown[pm] = payment_breakdown.get(pm, 0) + 1

        created = item.get("created_at")
        if created:
            day = created.strftime("%Y-%m-%d") if hasattr(created, "strftime") else str(created)[:10]
            daily_spending[day] = daily_spending.get(day, 0) + item.get("total_amount", 0)

    return {
        "period":              period,
        "total_spent":         round(total_spent, 2),
        "total_litres":        round(total_litres, 2),
        "transaction_count":   count,
        "avg_per_transaction": round(total_spent / count, 2) if count > 0 else 0.0,
        "fuel_breakdown":      fuel_breakdown,
        "payment_breakdown":   payment_breakdown,
        "daily_spending":      [
            {"date": k, "amount": round(v, 2)}
            for k, v in sorted(daily_spending.items())
        ],
    }


@router.get("/my-comparative")
async def my_comparative(
    period: str = Query("monthly", enum=["daily", "weekly", "monthly"]),
    current_user: dict = Depends(get_current_user),
):
    """Current-period vs previous-period comparison for the current user."""
    db = get_db()
    curr_start, curr_end = _date_range(period)

    if period == "monthly":
        prev_end   = curr_start
        prev_start = (prev_end.replace(day=1) - timedelta(days=1)).replace(day=1)
    else:
        delta      = curr_end - curr_start
        prev_end   = curr_start
        prev_start = prev_end - delta

    # Fetch all user transactions once (single-field equality = no composite index needed).
    all_items = [d.to_dict() for d in
                 db.collection(Collections.TRANSACTIONS).where("user_id", "==", current_user["uid"]).get()]

    def _agg_from_items(items: list[dict], start: datetime, end: datetime) -> dict:
        txns = _filter_by_period(items, "all")  # already fetched, filter manually
        period_txns = []
        for x in txns:
            created = x.get("created_at")
            if created is None:
                continue
            if hasattr(created, "tzinfo") and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if start <= created <= end:
                period_txns.append(x)
        return {
            "total_spent":  round(sum(x.get("total_amount", 0) for x in period_txns), 2),
            "total_litres": round(sum(x.get("litres_dispensed", 0) for x in period_txns), 2),
            "count":        len(period_txns),
        }

    def _pct(curr: float, prev: float) -> float:
        if prev == 0:
            return 0.0
        return round((curr - prev) / prev * 100, 1)

    current  = _agg_from_items(all_items, curr_start, curr_end)
    previous = _agg_from_items(all_items, prev_start, prev_end)

    return {
        "period":   period,
        "current":  current,
        "previous": previous,
        "change_pct": {
            "spent":  _pct(current["total_spent"],  previous["total_spent"]),
            "litres": _pct(current["total_litres"], previous["total_litres"]),
            "count":  _pct(float(current["count"]), float(previous["count"])),
        },
    }


@router.post("/my-export")
async def my_export(
    fmt:    str = Query("pdf", alias="format", enum=["pdf", "csv"]),
    period: str = Query("monthly", enum=["daily", "weekly", "monthly", "all"]),
    current_user: dict = Depends(get_current_user),
):
    """Export the current user's transactions as PDF or CSV."""
    db = get_db()
    all_items = [{"id": d.id, **d.to_dict()} for d in
                 db.collection(Collections.TRANSACTIONS).where("user_id", "==", current_user["uid"]).get()]
    filtered  = _filter_by_period(all_items, period)
    raw_items = sorted(
        filtered,
        key=lambda x: x.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    # Sanitize: convert datetime objects to strings for serialisation
    clean_items = []
    for item in raw_items:
        clean = {
            "Date":             item.get("created_at").strftime("%Y-%m-%d %H:%M UTC")
                                if hasattr(item.get("created_at"), "strftime")
                                else str(item.get("created_at", ""))[:16],
            "Fuel Type":        item.get("fuel_type", "").title(),
            "Litres":           round(item.get("litres_dispensed", 0), 2),
            "Price/L (PKR)":    round(item.get("price_per_litre", 0), 2),
            "Total (PKR)":      round(item.get("total_amount", 0), 2),
            "Payment":          item.get("payment_method", "").replace("_", " ").title(),
            "Status":           item.get("status", "").title(),
            "Transaction ID":   item.get("id", "")[-12:],
        }
        clean_items.append(clean)

    data = {
        "items":              clean_items,
        "total_transactions": len(clean_items),
        "period":             period,
    }

    if fmt == "pdf":
        content    = export_report_to_pdf(data, "My Transactions")
        media_type = "application/pdf"
        filename   = f"my_transactions_{period}.pdf"
    else:
        content    = export_report_to_csv(data, "my_transactions")
        media_type = "text/csv"
        filename   = f"my_transactions_{period}.csv"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
