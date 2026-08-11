"""
計算引擎 — 純函式，完全不碰資料庫，因此可以單獨測試。

三條公式，就這樣：

  1. 熱值換算   kg氣體/活動單位 = 公告係數(kg/TJ) × 4.1868E-9 × 熱值(kcal/活動單位)
  2. 溫室氣體加總  kgCO2e/單位   = CO2×1 + CH4×GWP_CH4 + N2O×GWP_N2O
  3. 排放量      kgCO2e         = 分攤後活動數據 × 每單位合計係數

電力是例外：官方係數已是合併 CO2e，直接乘活動數據，不套 GWP。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# 附表一 註2：1 千卡(kcal) = 4.1868×10⁻⁹ 兆焦耳(TJ)
KCAL_TO_TJ = 4.1868e-9

UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    ("公升", "公升"): 1.0, ("L", "公升"): 1.0,
    ("公秉", "公升"): 1000.0,
    ("度", "度"): 1.0, ("kWh", "度"): 1.0, ("千度", "度"): 1000.0,
    ("立方公尺", "立方公尺"): 1.0, ("m3", "立方公尺"): 1.0,
    ("公斤", "公斤"): 1.0, ("kg", "公斤"): 1.0, ("公噸", "公斤"): 1000.0,
}


class UnitMismatchError(ValueError):
    """單位無法換算時拋出。絕不靜默假設 1:1。"""


class MissingHeatingValueError(ValueError):
    """燃料類缺熱值就算不出每單位係數，必須明確失敗。"""


# --------------------------------------------------------------------------
# 跨期分攤
# --------------------------------------------------------------------------

@dataclass
class Allocation:
    total_days: int
    days_in_year: int
    ratio: float
    allocated_quantity: float
    is_cross_period: bool

    @property
    def quality_hint(self) -> str:
        """跨期分攤後的數字本質上是推估，提示服務層標記。"""
        return "推估" if self.is_cross_period else "實測"


def allocate_period(
    quantity: float,
    period_start: dt.date,
    period_end: dt.date,
    year_start: dt.date,
    year_end: dt.date,
) -> Allocation:
    """
    帳單期間常跨盤查年度（雙月期電費單每年至少發生一次）。
    依落在盤查年度內的天數比例分攤。
    """
    if period_end < period_start:
        raise ValueError("期間結束日不可早於開始日")
    if quantity < 0:
        raise ValueError("活動數據不可為負值")

    total_days = (period_end - period_start).days + 1
    overlap_start = max(period_start, year_start)
    overlap_end = min(period_end, year_end)
    days_in_year = max(0, (overlap_end - overlap_start).days + 1)
    ratio = days_in_year / total_days if total_days else 0.0

    return Allocation(
        total_days=total_days,
        days_in_year=days_in_year,
        ratio=ratio,
        allocated_quantity=quantity * ratio,
        is_cross_period=days_in_year != total_days,
    )


def convert_quantity(quantity: float, from_unit: str, to_unit: str) -> float:
    if from_unit == to_unit:
        return quantity
    ratio = UNIT_CONVERSIONS.get((from_unit, to_unit))
    if ratio is None:
        raise UnitMismatchError(f"無法將「{from_unit}」換算為「{to_unit}」")
    return quantity * ratio


# --------------------------------------------------------------------------
# 係數推導
# --------------------------------------------------------------------------

@dataclass
class FuelFactorInput:
    """對應 PublishedFactor，攤平成純資料"""
    factor_key: str
    display_name: str
    co2_kg_per_tj: float
    ch4_kg_per_tj: float
    n2o_kg_per_tj: float
    ch4_gwp_gas: str = "甲烷"
    source_ref: str = ""
    factor_set_version: str = ""


@dataclass
class DerivedFactor:
    """由公告係數與熱值推導出的每單位係數"""
    unit: str
    co2_per_unit: float
    ch4_per_unit: float
    n2o_per_unit: float
    total_co2e_per_unit: float
    heating_value: float
    heating_value_source: str
    ch4_gwp: float
    n2o_gwp: float
    trace: list[str] = field(default_factory=list)


def derive_fuel_factor(
    factor: FuelFactorInput,
    heating_value_kcal: float | None,
    heating_value_unit: str,
    heating_value_source: str,
    gwp: dict[str, float],
) -> DerivedFactor:
    """
    公告係數(kg/TJ) × 4.1868E-9 × 熱值(kcal/單位) = kg氣體/單位

    heating_value 缺失時直接失敗，不猜、不給預設值 —— 因為猜出來的
    數字看起來一樣合理，但錯了沒人發現。
    """
    if heating_value_kcal is None or heating_value_kcal <= 0:
        raise MissingHeatingValueError(
            f"燃料「{factor.display_name}」缺少有效熱值，無法推導每單位係數"
        )

    ch4_gwp = gwp.get(factor.ch4_gwp_gas)
    n2o_gwp = gwp.get("氧化亞氮")
    if ch4_gwp is None or n2o_gwp is None:
        raise ValueError(f"GWP 缺少「{factor.ch4_gwp_gas}」或「氧化亞氮」")

    k = KCAL_TO_TJ * heating_value_kcal
    co2 = factor.co2_kg_per_tj * k
    ch4 = factor.ch4_kg_per_tj * k
    n2o = factor.n2o_kg_per_tj * k
    total = co2 * 1.0 + ch4 * ch4_gwp + n2o * n2o_gwp

    trace = [
        f"係數推導（{factor.display_name}，{factor.source_ref}）",
        f"  熱值 {heating_value_kcal:g} kcal/{heating_value_unit}"
        f"（{heating_value_source}）",
        f"  CO2: {factor.co2_kg_per_tj:g} kg/TJ × {KCAL_TO_TJ:.4e}"
        f" × {heating_value_kcal:g} = {co2:.8f} kg/{heating_value_unit}",
        f"  CH4: {factor.ch4_kg_per_tj:g} kg/TJ → {ch4:.8f}"
        f" × GWP {ch4_gwp:g}（{factor.ch4_gwp_gas}）",
        f"  N2O: {factor.n2o_kg_per_tj:g} kg/TJ → {n2o:.8f} × GWP {n2o_gwp:g}",
        f"  每單位合計 = {total:.6f} kgCO2e/{heating_value_unit}",
    ]
    return DerivedFactor(
        unit=heating_value_unit,
        co2_per_unit=co2, ch4_per_unit=ch4, n2o_per_unit=n2o,
        total_co2e_per_unit=total,
        heating_value=heating_value_kcal,
        heating_value_source=heating_value_source,
        ch4_gwp=ch4_gwp, n2o_gwp=n2o_gwp, trace=trace,
    )


# --------------------------------------------------------------------------
# 排放量
# --------------------------------------------------------------------------

@dataclass
class CalcResult:
    co2_kg: float = 0.0
    ch4_co2e_kg: float = 0.0
    n2o_co2e_kg: float = 0.0
    total_co2e_kg: float = 0.0
    derived_factor: float = 0.0
    trace: list[str] = field(default_factory=list)

    @property
    def total_tco2e(self) -> float:
        return self.total_co2e_kg / 1000.0

    @property
    def trace_text(self) -> str:
        return "\n".join(self.trace)


def calculate_fuel(
    quantity: float, unit: str, derived: DerivedFactor,
) -> CalcResult:
    qty = convert_quantity(quantity, unit, derived.unit)
    co2 = qty * derived.co2_per_unit
    ch4 = qty * derived.ch4_per_unit * derived.ch4_gwp
    n2o = qty * derived.n2o_per_unit * derived.n2o_gwp
    total = co2 + ch4 + n2o

    trace = list(derived.trace) + [
        f"排放量計算：{quantity:g} {unit} → {qty:g} {derived.unit}",
        f"  CO2 {co2:,.4f} + CH4 {ch4:,.4f} + N2O {n2o:,.4f}"
        f" = {total:,.4f} kgCO2e（{total / 1000:,.6f} tCO2e）",
    ]
    return CalcResult(co2, ch4, n2o, total, derived.total_co2e_per_unit, trace)


def calculate_electricity(
    quantity: float, unit: str, kgco2e_per_kwh: float, year_roc: int,
    source: str = "",
) -> CalcResult:
    """電力係數已含 GWP，直接相乘。再乘一次 GWP 是常見錯誤。"""
    qty = convert_quantity(quantity, unit, "度")
    total = qty * kgco2e_per_kwh
    trace = [
        f"外購電力（{year_roc}年度，{source}）",
        f"  {qty:g} 度 × {kgco2e_per_kwh:g} kgCO2e/度 = {total:,.4f} kgCO2e",
        f"  （{total / 1000:,.6f} tCO2e）係數已含 GWP，不再套用",
    ]
    return CalcResult(co2_kg=total, total_co2e_kg=total,
                      derived_factor=kgco2e_per_kwh, trace=trace)


# --------------------------------------------------------------------------
# 完整性檢查
# --------------------------------------------------------------------------

@dataclass
class CompletenessIssue:
    source_no: str
    source_name: str
    issue: str
    severity: str      # error / warning


def check_completeness(
    sources: list[tuple[str, str, bool]],
    records_by_source: dict[str, list[tuple[dt.date, dt.date]]],
    year_start: dt.date,
    year_end: dt.date,
) -> list[CompletenessIssue]:
    """
    sources: [(source_no, name, active), ...]
    records_by_source: {source_no: [(period_start, period_end), ...]}

    抓兩種漏洞：清冊上有排放源卻完全沒資料；有資料但年度中有月份沒被覆蓋。
    後者是最陰險的 —— 少一張帳單，總量少一截，卻不會有任何錯誤訊息。
    """
    issues: list[CompletenessIssue] = []
    for source_no, name, active in sources:
        if not active:
            continue
        periods = records_by_source.get(source_no, [])
        if not periods:
            issues.append(CompletenessIssue(
                source_no, name,
                "清冊列有此排放源，但全年無任何活動數據。"
                "若年度確實未使用，請填 0 並註明，不可留空。", "error"))
            continue

        covered = set()
        for ps, pe in periods:
            s, e = max(ps, year_start), min(pe, year_end)
            d = s
            while d <= e:
                covered.add((d.year, d.month))
                d = (dt.date(d.year + 1, 1, 1) if d.month == 12
                     else dt.date(d.year, d.month + 1, 1))
        missing = [m for m in range(1, 13)
                   if (year_start.year, m) not in covered]
        if missing:
            issues.append(CompletenessIssue(
                source_no, name,
                f"缺少月份：{'、'.join(f'{m}月' for m in missing)}", "warning"))
    return issues
