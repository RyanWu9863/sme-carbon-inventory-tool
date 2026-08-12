"""
服務層 — 把資料庫接上計算引擎。

`calculator.py` 是純函式，收的是攤平的 dataclass；`models.py` 是 ORM 物件。
這一層做三件事：

    1. 查係數   factor_key + 盤查年度 → 公告係數／熱值／GWP，組成計算引擎要的輸入
    2. 算一筆   ActivityRecord → 跨期分攤 → 計算 → EmissionResult（含稽核快照）
    3. 算年度   彙總成表八，並跑完整性檢查

三個設計重點：

**固定／移動燃燒依表三的「排放型式」拆，不是依燃料種類。**
柴油在示範案例裡同時出現在固定燃燒（備用發電機）與移動燃燒（公務車），
依燃料猜一定猜錯。`EmissionSource.emission_type` 才是唯一依據。

**推估必須填理由，由這一層強制。**
資料庫沒有這個約束（`estimation_basis` 是 nullable），models.py 註明「由服務
層強制」。跨期分攤出來的數字本質上是推估，這一層會一併檢查。

**每一筆結果都釘住當下用的係數版本。**
`EmissionResult` 那一排快照欄位不是裝飾。公告改版後，舊報告的數字要還原得
出來，靠的就是它們。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .calculator import (
    CalcResult, CompletenessIssue, DerivedFactor, FuelFactorInput, allocate_period,
    calculate_electricity, calculate_fuel, check_completeness, derive_fuel_factor,
)
from .models import (
    ActivityRecord, DataQuality, ElectricityFactor, EmissionResult, EmissionSource,
    EmissionType, FactorSet, GwpValue, HeatingValue, Organization, PublishedFactor,
    utcnow,
)

# 走燃料路徑的排放型式。其餘（製程、逸散、外購蒸汽）目前不支援，
# 但要明確報錯而不是算成 0 —— 算成 0 會讓報告少一截卻沒人發現。
_FUEL_TYPES = {EmissionType.STATIONARY, EmissionType.MOBILE}


class ServiceError(RuntimeError):
    """服務層拒絕計算。訊息要講清楚缺什麼、該怎麼補。"""


class FactorNotFoundError(ServiceError):
    pass


class DataQualityError(ServiceError):
    pass


@dataclass(frozen=True)
class ResolvedFuelFactor:
    """一個燃料排放源要用的全部東西，連同溯源資訊。"""

    published: PublishedFactor
    heating: HeatingValue
    derived: DerivedFactor
    factor_set_version: str


@dataclass
class YearSummary:
    """表八所需的全部數字。單位一律 tCO2e。"""

    org_name: str
    year_roc: int
    by_emission_type: dict[str, float] = field(default_factory=dict)
    by_gas: dict[str, float] = field(default_factory=dict)
    scope1_tco2e: float = 0.0
    scope2_tco2e: float = 0.0
    total_tco2e: float = 0.0
    measured_count: int = 0
    estimated_count: int = 0
    estimated_tco2e: float = 0.0
    issues: list[CompletenessIssue] = field(default_factory=list)
    results: list[EmissionResult] = field(default_factory=list)

    @property
    def estimated_share(self) -> float:
        """推估排放量占比。報告必列（表八「資料品質揭露」）。"""
        return self.estimated_tco2e / self.total_tco2e if self.total_tco2e else 0.0

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


# --------------------------------------------------------------------------
# 查係數
# --------------------------------------------------------------------------

def gwp_table(session: Session, standard: str = "AR5") -> dict[str, float]:
    rows = session.scalars(
        select(GwpValue).where(GwpValue.standard == standard)).all()
    if not rows:
        raise FactorNotFoundError(
            f"資料庫沒有 {standard} 的 GWP 值。請先執行 python scripts/import_seed.py")
    return {r.gas_name: r.gwp100 for r in rows}


def resolve_heating_value(session: Session, material_code: str,
                          org_id: int | None) -> HeatingValue:
    """
    事業自填的熱值優先於系統預設。

    models.py 明講熱值是唯一「使用者可以合法覆寫官方值」的資料 —— 環境部原意
    就是由事業填入自身燃料實際熱值。優先順序寫在這裡，是因為它是查詢邏輯，
    不是資料本身的性質。
    """
    if org_id is not None:
        own = session.scalars(
            select(HeatingValue).where(
                HeatingValue.material_code == material_code,
                HeatingValue.org_id == org_id,
            )
        ).first()
        if own is not None:
            return own

    default = session.scalars(
        select(HeatingValue).where(
            HeatingValue.material_code == material_code,
            HeatingValue.org_id.is_(None),
        )
    ).first()
    if default is None:
        raise FactorNotFoundError(
            f"找不到原燃物料代碼 {material_code} 的熱值。"
            f"沒有熱值就推導不出每單位係數 —— 請確認種子資料已匯入。"
        )
    return default


def resolve_fuel_factor(session: Session, factor_key: str,
                        org_id: int | None = None) -> ResolvedFuelFactor:
    """給係數編號（TW-F-NG-S），組出計算引擎要的每單位係數。"""
    published = session.scalars(
        select(PublishedFactor).where(PublishedFactor.factor_key == factor_key)
    ).first()
    if published is None:
        raise FactorNotFoundError(
            f"找不到係數編號「{factor_key}」。"
            f"請確認表三的「對應係數編號」與附表一一致。"
        )

    heating = resolve_heating_value(session, published.material_code, org_id)
    factor_set = session.get(FactorSet, published.factor_set_id)
    gwp = gwp_table(session, factor_set.gwp_standard if factor_set else "AR5")

    derived = derive_fuel_factor(
        FuelFactorInput(
            factor_key=published.factor_key,
            display_name=published.display_name,
            co2_kg_per_tj=published.co2_kg_per_tj,
            ch4_kg_per_tj=published.ch4_kg_per_tj,
            n2o_kg_per_tj=published.n2o_kg_per_tj,
            ch4_gwp_gas=published.ch4_gwp_gas,
            source_ref=published.source_ref or "",
            factor_set_version=factor_set.version if factor_set else "",
        ),
        heating_value_kcal=heating.kcal_per_unit,
        heating_value_unit=heating.unit,
        heating_value_source=heating.source,
        gwp=gwp,
    )
    return ResolvedFuelFactor(
        published=published,
        heating=heating,
        derived=derived,
        factor_set_version=factor_set.version if factor_set else "",
    )


def resolve_electricity_factor(session: Session, year_roc: int) -> ElectricityFactor:
    """
    電力係數逐年公告，必須與盤查年度一致。

    查不到時要分清楚兩種情況：政府還沒公告，跟資料庫沒匯入。使用者能做的事
    完全不同，講「查無係數」等於什麼都沒講。
    """
    factor = session.scalars(
        select(ElectricityFactor).where(ElectricityFactor.year_roc == year_roc)
    ).one_or_none()
    if factor is not None:
        return factor

    available = sorted(
        y for (y,) in session.execute(select(ElectricityFactor.year_roc)).all())
    if available and year_roc > max(available):
        raise FactorNotFoundError(
            f"{year_roc} 年度的電力排碳係數尚未公告（目前最新是 {max(available)} 年度）。"
            f"這不是程式或資料庫的問題 —— 能源署逐年公告，要等公告後才能盤查該年度。"
        )
    raise FactorNotFoundError(
        f"資料庫沒有 {year_roc} 年度的電力排碳係數"
        f"（現有：{'、'.join(map(str, available)) or '無'}）。"
        f"請確認已執行 python scripts/import_seed.py"
    )


# --------------------------------------------------------------------------
# 算一筆
# --------------------------------------------------------------------------

def _check_data_quality(record: ActivityRecord, source: EmissionSource,
                        is_cross_period: bool) -> None:
    """
    推估必須填理由。models.py 註明這條由服務層強制。

    為什麼是這一層而不是資料庫約束：跨期分攤之後才知道一筆資料是不是推估，
    那是計算過程產生的事實，資料庫寫入的當下還不知道。
    """
    label = f"{source.source_no} {source.name}（{record.evidence_ref or '無佐證編號'}）"

    if is_cross_period and record.data_quality != DataQuality.ESTIMATED:
        raise DataQualityError(
            f"{label} 的帳單期間跨出盤查年度，分攤後的數字本質上是推估，"
            f"資料品質卻標記為「{record.data_quality.value}」。請改為「推估」並填寫依據。"
        )

    if record.data_quality == DataQuality.ESTIMATED and not (record.estimation_basis or "").strip():
        raise DataQualityError(
            f"{label} 標記為推估卻沒有填推估依據。"
            f"推估本身沒問題，說不出怎麼推的才有問題 —— 那是查驗第一個會問的。"
        )


def calculate_record(session: Session, record: ActivityRecord,
                     org: Organization | None = None) -> EmissionResult:
    """
    算一筆活動數據，寫回 EmissionResult（含稽核快照）。

    同一筆重算會更新既有的 EmissionResult，不會長出第二筆 —— record_id 上有
    unique 約束，而且報告重跑本來就該覆蓋舊結果。
    """
    source = record.source or session.get(EmissionSource, record.source_id)
    if source is None:
        raise ServiceError(f"活動數據 id={record.id} 找不到對應的排放源")
    org = org or session.get(Organization, source.org_id)
    if org is None:
        raise ServiceError(f"排放源 {source.source_no} 找不到對應的事業")

    allocation = allocate_period(
        record.raw_quantity, record.period_start, record.period_end,
        org.year_start, org.year_end,
    )
    _check_data_quality(record, source, allocation.is_cross_period)

    record.allocation_ratio = allocation.ratio
    record.allocated_quantity = allocation.allocated_quantity

    snapshot: dict = {"gwp_standard": None}

    if source.emission_type == EmissionType.PURCHASED_ELECTRICITY:
        factor = resolve_electricity_factor(session, org.reporting_year_roc)
        calc: CalcResult = calculate_electricity(
            allocation.allocated_quantity, record.unit,
            factor.kgco2e_per_kwh, org.reporting_year_roc, factor.source or "",
        )
        snapshot |= {
            "electricity_factor_used": factor.kgco2e_per_kwh,
            "published_factor_id": None,
            "heating_value_used": None,
            "heating_value_source": None,
            "factor_set_version": None,
            "ch4_gwp_used": None,
            "n2o_gwp_used": None,
        }
    elif source.emission_type in _FUEL_TYPES:
        if not source.factor_key:
            raise FactorNotFoundError(
                f"排放源 {source.source_no}「{source.name}」沒有指定對應係數編號")
        resolved = resolve_fuel_factor(session, source.factor_key, org.id)
        calc = calculate_fuel(
            allocation.allocated_quantity, record.unit, resolved.derived)
        snapshot |= {
            "electricity_factor_used": None,
            "published_factor_id": resolved.published.id,
            "heating_value_used": resolved.heating.kcal_per_unit,
            "heating_value_source": resolved.heating.source,
            "factor_set_version": resolved.factor_set_version,
            "ch4_gwp_used": resolved.derived.ch4_gwp,
            "n2o_gwp_used": resolved.derived.n2o_gwp,
            "gwp_standard": "AR5",
        }
    else:
        raise ServiceError(
            f"排放源 {source.source_no}「{source.name}」的排放型式是"
            f"「{source.emission_type.value}」，本系統目前只涵蓋固定燃燒、移動燃燒與"
            f"外購電力。不支援的型式一律報錯而不算成 0 —— 算成 0 會讓報告少一截"
            f"卻沒有任何提示。"
        )

    trace = list(calc.trace)
    if allocation.is_cross_period:
        trace.insert(0, (
            f"跨期分攤：{record.period_start}~{record.period_end} 共 "
            f"{allocation.total_days} 天，落在盤查年度內 {allocation.days_in_year} 天，"
            f"比例 {allocation.ratio:.6f} → 分攤後標記為推估"
        ))

    result = record.result or session.scalars(
        select(EmissionResult).where(EmissionResult.record_id == record.id)
    ).one_or_none()
    if result is None:
        result = EmissionResult(record_id=record.id)
        session.add(result)

    result.co2_kg = calc.co2_kg
    result.ch4_co2e_kg = calc.ch4_co2e_kg
    result.n2o_co2e_kg = calc.n2o_co2e_kg
    result.total_co2e_kg = calc.total_co2e_kg
    result.derived_factor = calc.derived_factor
    result.calc_trace = "\n".join(trace)
    result.calculated_at = utcnow()
    for key, value in snapshot.items():
        setattr(result, key, value)

    return result


# --------------------------------------------------------------------------
# 算年度
# --------------------------------------------------------------------------

def calculate_year(session: Session, org: Organization) -> YearSummary:
    """
    算完一整年，彙總成表八。

    彙總依表三的「排放型式」拆分，而不是依燃料種類 —— 試算表把範疇一全掛在
    「固定燃燒」，表八 B11 自己註明程式版該拆開，這裡就是做那件事的地方。
    """
    sources = session.scalars(
        select(EmissionSource).where(EmissionSource.org_id == org.id)
        .order_by(EmissionSource.source_no)
    ).all()

    summary = YearSummary(org_name=org.name, year_roc=org.reporting_year_roc)
    periods: dict[str, list[tuple]] = {}

    for source in sources:
        records = session.scalars(
            select(ActivityRecord).where(ActivityRecord.source_id == source.id)
            .order_by(ActivityRecord.period_start)
        ).all()
        periods[source.source_no] = [(r.period_start, r.period_end) for r in records]

        if not source.active:
            continue

        for record in records:
            result = calculate_record(session, record, org)
            summary.results.append(result)

            tonnes = result.total_co2e_kg / 1000.0
            label = source.emission_type.value
            summary.by_emission_type[label] = (
                summary.by_emission_type.get(label, 0.0) + tonnes)

            summary.by_gas["CO2"] = summary.by_gas.get("CO2", 0.0) + result.co2_kg / 1000.0
            summary.by_gas["CH4"] = summary.by_gas.get("CH4", 0.0) + result.ch4_co2e_kg / 1000.0
            summary.by_gas["N2O"] = summary.by_gas.get("N2O", 0.0) + result.n2o_co2e_kg / 1000.0

            if source.direct_indirect.value == "直接":
                summary.scope1_tco2e += tonnes
            else:
                summary.scope2_tco2e += tonnes

            if record.data_quality == DataQuality.ESTIMATED:
                summary.estimated_count += 1
                summary.estimated_tco2e += tonnes
            else:
                summary.measured_count += 1

    summary.total_tco2e = summary.scope1_tco2e + summary.scope2_tco2e
    summary.issues = check_completeness(
        [(s.source_no, s.name, bool(s.active)) for s in sources],
        periods, org.year_start, org.year_end,
    )
    return summary


def format_summary(summary: YearSummary) -> str:
    """把 YearSummary 印成表八的樣子。"""
    lines = [
        f"{summary.org_name}　{summary.year_roc} 年度溫室氣體排放量彙總",
        "",
        "【依排放型式】",
    ]
    for label, tonnes in sorted(summary.by_emission_type.items()):
        lines.append(f"  {label:<10} {tonnes:>12.6f} tCO2e")

    lines += [
        "",
        "【依範疇】",
        f"  範疇一 直接排放     {summary.scope1_tco2e:>12.6f} tCO2e",
        f"  範疇二 外購電力     {summary.scope2_tco2e:>12.6f} tCO2e",
        f"  範疇三 其他間接         {0.0:>12.6f} tCO2e　（本系統不涵蓋，報告須揭露）",
        f"  {'總計':<16} {summary.total_tco2e:>12.6f} tCO2e",
        "",
        "【依氣體】",
    ]
    for gas in ("CO2", "CH4", "N2O"):
        value = summary.by_gas.get(gas, 0.0)
        share = value / summary.total_tco2e if summary.total_tco2e else 0.0
        lines.append(f"  {gas:<5} {value:>12.6f} tCO2e　{share:>7.2%}")

    lines += [
        "",
        "【資料品質】",
        f"  實測 {summary.measured_count} 筆　推估 {summary.estimated_count} 筆",
        f"  推估排放量占比 {summary.estimated_share:.2%}",
    ]

    if summary.issues:
        lines += ["", "【完整性檢查】"]
        for issue in summary.issues:
            mark = "✗" if issue.severity == "error" else "!"
            lines.append(f"  {mark} {issue.source_no} {issue.source_name}：{issue.issue}")
    else:
        lines += ["", "【完整性檢查】無問題"]

    return "\n".join(lines)
