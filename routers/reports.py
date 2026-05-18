from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Response, status

from core.dependencies import require_admin
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
