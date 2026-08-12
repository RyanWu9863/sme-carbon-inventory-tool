"""
HTTP API（FastAPI）。

    uvicorn app.api:app --reload
    → http://127.0.0.1:8000/docs

這一層刻意很薄：所有判斷都在 `service.py`，這裡只做三件事 —— 把 HTTP 請求
轉成服務層呼叫、把 ORM 物件轉成 JSON、把服務層的例外轉成 HTTP 回應。
API 裡不該出現任何一行計算。

兩個設計決定：

**GET 不寫資料庫。**
`GET /orgs/{id}/summary` 讀已經算好的結果（`stored_summary`），
`POST /orgs/{id}/calculate` 才會重算並寫入。一開始把兩者合在一個 GET 裡很方便，
但那表示每次重新整理報表頁都在改資料庫 —— 包括 `calculated_at` 時間戳記，
而那正是稽核要看的東西。

**錯誤帶機器可讀的 code。**
HTTP 狀態碼分不出「係數還沒公告」跟「係數編號打錯」，但前端該做的事完全不同：
一個顯示「等公告」，一個要跳到表三讓使用者改。所以回應主體帶 `code`
（見 `ServiceError.code`），狀態碼只表達粗分類。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path as FilePath

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db
from .models import (
    ActivityRecord, DataQuality, EmissionSource, EquipmentCode, EvidenceType,
    FactorSet, MaterialCode, Organization, ProcessCode, PublishedFactor,
)
from .service import (
    ServiceError, YearSummary, calculate_record, calculate_year, stored_summary,
)

app = FastAPI(
    title="中小企業碳盤查工具",
    description=(
        "對齊環境部「溫室氣體排放量清冊表單」。"
        "計算邏輯在 app/calculator.py（純函式），查詢與組裝在 app/service.py。"
    ),
    version="0.1.0",
)


# --------------------------------------------------------------------------
# 錯誤處理
# --------------------------------------------------------------------------

@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    """
    服務層的拒絕一律回 422，細節放在 code。

    422 而不是 400：請求格式是對的，是資料本身不合格。前端要靠 code 決定
    畫面 —— factor_not_published 該說「等公告」，data_quality 該把使用者
    帶回那一筆活動數據。
    """
    return JSONResponse(
        status_code=422,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )


def _get_org(db: Session, org_id: int) -> Organization:
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail=f"找不到 id={org_id} 的事業")
    return org


# --------------------------------------------------------------------------
# 介面
# --------------------------------------------------------------------------

_UI_FILE = FilePath(__file__).resolve().parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def index():
    """
    使用者介面。單一 HTML 檔，沒有 build step。

    刻意不用 React／Vue：這個作品的重點在盤查邏輯與資料正確性，不在前端工程。
    多一個 build step，就多一個「在我電腦上跑得起來」的理由 —— demo 影片要能
    只靠一個網址播完。
    """
    if not _UI_FILE.exists():
        raise HTTPException(status_code=404, detail=f"找不到介面檔案：{_UI_FILE}")
    return FileResponse(_UI_FILE, media_type="text/html; charset=utf-8")


# --------------------------------------------------------------------------
# 輸出格式
# --------------------------------------------------------------------------

class OrgOut(BaseModel):
    id: int
    name: str
    reporting_year_roc: int
    year_start: dt.date
    year_end: dt.date
    boundary_method: str | None = None
    county: str | None = None
    district: str | None = None


class SourceOut(BaseModel):
    """
    表三一列。

    代碼旁邊一律附上名稱。官方表三本來就是「代碼＋名稱」兩欄並列，
    只回 `9999` 而不回「其他未歸類設施」的話，畫面上沒有人看得懂，
    而看不懂的欄位使用者就會亂填。
    """

    source_no: str
    name: str
    direct_indirect: str
    emission_type: str
    process_code: str | None = None
    process_name: str | None = None
    equipment_code: str | None = None
    equipment_name: str | None = None
    material_code: str | None = None
    material_name: str | None = None
    factor_key: str | None = None
    active: bool
    record_count: int
    note: str | None = None


class RecordIn(BaseModel):
    """新增一筆活動數據。分攤與排放量由服務層算，不接受前端傳。"""

    period_start: dt.date
    period_end: dt.date
    raw_quantity: float = Field(ge=0, description="單據上的原始數字，不可為負")
    unit: str
    data_quality: str = Field(default="實測", pattern="^(實測|推估)$")
    estimation_basis: str | None = Field(
        default=None, description="推估時必填，服務層會擋")
    evidence_type: str = "手動輸入"
    evidence_ref: str | None = None


class RecordOut(BaseModel):
    id: int
    source_no: str
    period_start: dt.date
    period_end: dt.date
    raw_quantity: float
    unit: str
    allocation_ratio: float | None = None
    allocated_quantity: float | None = None
    data_quality: str
    estimation_basis: str | None = None
    evidence_ref: str | None = None
    total_co2e_kg: float | None = None
    calc_trace: str | None = None


class IssueOut(BaseModel):
    source_no: str
    source_name: str
    issue: str
    severity: str


class SummaryOut(BaseModel):
    """表八。"""

    org_name: str
    year_roc: int
    by_emission_type: dict[str, float]
    by_gas: dict[str, float]
    scope1_tco2e: float
    scope2_tco2e: float
    total_tco2e: float
    measured_count: int
    estimated_count: int
    estimated_share: float
    uncalculated_count: int
    has_errors: bool
    issues: list[IssueOut]


class CodeOut(BaseModel):
    code: str
    name: str


class CodeSearchOut(BaseModel):
    """
    帶總數與是否截斷。

    只回一個陣列的話，使用者搜「油」拿到 50 筆會以為就是全部 —— 實際上可能
    有 300 筆，他要的在第 51 筆。truncated 讓前端有辦法說「還有更多，請再細一點」。
    """

    kind: str
    query: str | None
    total: int
    returned: int
    truncated: bool
    items: list[CodeOut]


class FactorOut(BaseModel):
    factor_key: str
    display_name: str
    usage: str
    vehicle_tech: str | None = None
    material_code: str | None = None
    source_ref: str | None = None
    factor_set_version: str | None = None


# --------------------------------------------------------------------------
# 轉換
# --------------------------------------------------------------------------

def _org_out(org: Organization) -> OrgOut:
    return OrgOut(
        id=org.id, name=org.name, reporting_year_roc=org.reporting_year_roc,
        year_start=org.year_start, year_end=org.year_end,
        boundary_method=org.boundary_method, county=org.county, district=org.district,
    )


def _record_out(record: ActivityRecord, source_no: str) -> RecordOut:
    result = record.result
    return RecordOut(
        id=record.id, source_no=source_no,
        period_start=record.period_start, period_end=record.period_end,
        raw_quantity=record.raw_quantity, unit=record.unit,
        allocation_ratio=record.allocation_ratio,
        allocated_quantity=record.allocated_quantity,
        data_quality=record.data_quality.value,
        estimation_basis=record.estimation_basis,
        evidence_ref=record.evidence_ref,
        total_co2e_kg=result.total_co2e_kg if result else None,
        calc_trace=result.calc_trace if result else None,
    )


def _summary_out(summary: YearSummary) -> SummaryOut:
    return SummaryOut(
        org_name=summary.org_name, year_roc=summary.year_roc,
        by_emission_type=summary.by_emission_type, by_gas=summary.by_gas,
        scope1_tco2e=summary.scope1_tco2e, scope2_tco2e=summary.scope2_tco2e,
        total_tco2e=summary.total_tco2e,
        measured_count=summary.measured_count,
        estimated_count=summary.estimated_count,
        estimated_share=summary.estimated_share,
        uncalculated_count=summary.uncalculated_count,
        has_errors=summary.has_errors,
        issues=[
            IssueOut(source_no=i.source_no, source_name=i.source_name,
                     issue=i.issue, severity=i.severity)
            for i in summary.issues
        ],
    )


# --------------------------------------------------------------------------
# 端點
# --------------------------------------------------------------------------

@app.get("/health", tags=["系統"])
def health(db: Session = Depends(get_db)) -> dict:
    """種子資料在不在。空資料庫算出來的 0 跟真的 0 長得一樣，要能分辨。"""
    factors = db.scalar(select(func.count()).select_from(PublishedFactor))
    materials = db.scalar(select(func.count()).select_from(MaterialCode))
    factor_set = db.scalars(select(FactorSet)).first()
    return {
        "status": "ok" if factors and materials else "種子資料未匯入",
        "published_factors": factors,
        "material_codes": materials,
        "factor_set": factor_set.version if factor_set else None,
        "hint": None if factors else "請執行 python scripts/import_seed.py",
    }


@app.get("/orgs", response_model=list[OrgOut], tags=["事業"])
def list_orgs(db: Session = Depends(get_db)):
    return [_org_out(o) for o in db.scalars(select(Organization).order_by(Organization.id))]


@app.get("/orgs/{org_id}", response_model=OrgOut, tags=["事業"])
def get_org(org_id: int, db: Session = Depends(get_db)):
    return _org_out(_get_org(db, org_id))


def _name_lookup(db: Session, model, codes: set[str]) -> dict[str, str]:
    """一次查完，不要在迴圈裡逐筆查資料庫。"""
    codes = {c for c in codes if c}
    if not codes:
        return {}
    rows = db.scalars(select(model).where(model.code.in_(codes))).all()
    return {r.code: r.name for r in rows}


@app.get("/orgs/{org_id}/sources", response_model=list[SourceOut], tags=["表三 排放源"])
def list_sources(org_id: int, db: Session = Depends(get_db)):
    org = _get_org(db, org_id)
    sources = db.scalars(
        select(EmissionSource).where(EmissionSource.org_id == org.id)
        .order_by(EmissionSource.source_no)
    ).all()

    processes = _name_lookup(db, ProcessCode, {s.process_code for s in sources})
    equipment = _name_lookup(db, EquipmentCode, {s.equipment_code for s in sources})
    materials = _name_lookup(db, MaterialCode, {s.material_code for s in sources})

    counts = dict(db.execute(
        select(ActivityRecord.source_id, func.count())
        .group_by(ActivityRecord.source_id)
    ).all())

    return [
        SourceOut(
            source_no=s.source_no, name=s.name,
            direct_indirect=s.direct_indirect.value,
            emission_type=s.emission_type.value,
            process_code=s.process_code,
            process_name=processes.get(s.process_code),
            equipment_code=s.equipment_code,
            equipment_name=equipment.get(s.equipment_code),
            material_code=s.material_code,
            material_name=materials.get(s.material_code),
            factor_key=s.factor_key,
            active=bool(s.active), note=s.note,
            record_count=counts.get(s.id, 0),
        )
        for s in sources
    ]


@app.get("/orgs/{org_id}/records", response_model=list[RecordOut], tags=["表四 活動數據"])
def list_records(org_id: int, db: Session = Depends(get_db)):
    org = _get_org(db, org_id)
    rows = db.execute(
        select(ActivityRecord, EmissionSource.source_no)
        .join(EmissionSource, ActivityRecord.source_id == EmissionSource.id)
        .where(EmissionSource.org_id == org.id)
        .order_by(EmissionSource.source_no, ActivityRecord.period_start)
    ).all()
    return [_record_out(rec, source_no) for rec, source_no in rows]


@app.post("/orgs/{org_id}/sources/{source_no}/records", response_model=RecordOut,
          status_code=201, tags=["表四 活動數據"])
def add_record(org_id: int, source_no: str, payload: RecordIn,
               db: Session = Depends(get_db)):
    """
    新增一筆活動數據，並立刻計算。

    立刻算是刻意的：推估沒填依據、跨年帳單標成實測、係數查不到 —— 這些都要
    在使用者還看著這張表單時就講，而不是等他按下「產生報告」才一次爆出來。
    計算失敗時整筆不寫入（回滾），不留下一筆算不出結果的資料。
    """
    org = _get_org(db, org_id)
    source = db.scalars(
        select(EmissionSource).where(
            EmissionSource.org_id == org.id,
            EmissionSource.source_no == source_no,
        )
    ).one_or_none()
    if source is None:
        raise HTTPException(
            status_code=404,
            detail=f"事業 {org.name} 沒有編號 {source_no} 的排放源")

    if payload.period_end < payload.period_start:
        raise HTTPException(status_code=422, detail="期間結束日不可早於開始日")

    record = ActivityRecord(
        source_id=source.id,
        period_start=payload.period_start, period_end=payload.period_end,
        raw_quantity=payload.raw_quantity, unit=payload.unit,
        data_quality=DataQuality(payload.data_quality),
        estimation_basis=payload.estimation_basis,
        evidence_type=EvidenceType(payload.evidence_type),
        evidence_ref=payload.evidence_ref,
        data_source=payload.evidence_type,
    )
    db.add(record)
    db.flush()

    try:
        calculate_record(db, record, org)
    except ServiceError:
        db.rollback()
        raise               # 交給 service_error_handler，帶 code 回去

    db.commit()
    db.refresh(record)
    return _record_out(record, source.source_no)


@app.get("/orgs/{org_id}/summary", response_model=SummaryOut, tags=["表八 彙總"])
def get_summary(org_id: int, db: Session = Depends(get_db)):
    """
    讀已經算好的結果。**不會重算，也不寫資料庫。**

    還沒算過的筆數在 `uncalculated_count`。它大於 0 表示這張表八是不完整的，
    要先 POST /calculate。
    """
    return _summary_out(stored_summary(db, _get_org(db, org_id)))


@app.post("/orgs/{org_id}/calculate", response_model=SummaryOut, tags=["表八 彙總"])
def recalculate(org_id: int, db: Session = Depends(get_db)):
    """重算整個年度並寫回 EmissionResult。"""
    org = _get_org(db, org_id)
    summary = calculate_year(db, org)
    db.commit()
    return _summary_out(summary)


_CODE_TABLES = {
    "process": (ProcessCode, "製程（附表五）"),
    "equipment": (EquipmentCode, "設備（附表六）"),
    "material": (MaterialCode, "原(燃)物料（附表七）"),
}


@app.get("/codes/{kind}", response_model=CodeSearchOut, tags=["官方代碼表"])
def search_codes(
    kind: str = Path(description="process / equipment / material"),
    q: str | None = Query(default=None, description="代碼或名稱的片段"),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """
    代碼查詢。

    原(燃)物料有 6,222 筆，不可能做成下拉選單，一定要能搜尋。回應帶 `total`
    與 `truncated` —— 只回一個陣列的話，使用者搜「油」拿到 50 筆會以為就是
    全部，實際上他要的可能在第 51 筆。
    """
    entry = _CODE_TABLES.get(kind)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"未知的代碼表「{kind}」，可用：{'、'.join(_CODE_TABLES)}")
    model, _label = entry

    stmt = select(model)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(model.code.like(pattern) | model.name.like(pattern))

    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(model.code).limit(limit)).all()

    return CodeSearchOut(
        kind=kind, query=q, total=total, returned=len(rows),
        truncated=total > len(rows),
        items=[CodeOut(code=r.code, name=r.name) for r in rows],
    )


@app.get("/factors", response_model=list[FactorOut], tags=["官方代碼表"])
def list_factors(db: Session = Depends(get_db)):
    """
    可用的燃料係數，供表三的「對應係數編號」選單使用。

    只有 12 個，不需要搜尋。電力不在這裡 —— 它走完全不同的路徑（係數已含
    GWP、逐年公告），混在同一個清單只會讓人選錯。
    """
    rows = db.execute(
        select(PublishedFactor, FactorSet.version)
        .join(FactorSet, PublishedFactor.factor_set_id == FactorSet.id)
        .order_by(PublishedFactor.id)
    ).all()
    return [
        FactorOut(
            factor_key=f.factor_key, display_name=f.display_name,
            usage=f.usage.value, vehicle_tech=f.vehicle_tech,
            material_code=f.material_code, source_ref=f.source_ref,
            factor_set_version=version,
        )
        for f, version in rows
    ]
