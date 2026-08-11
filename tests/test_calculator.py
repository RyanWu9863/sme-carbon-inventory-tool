"""
驗收測試：程式算出的數字，必須與 碳盤查試算表_v5.xlsx 完全一致。

這是 W2 的完成判準。跑得過，才算把試算表成功程式化。
"""

import datetime as dt

import pytest

from app.calculator import (
    FuelFactorInput, MissingHeatingValueError, UnitMismatchError,
    allocate_period, calculate_electricity, calculate_fuel,
    check_completeness, derive_fuel_factor,
)

# 附表二／附表四（AR5）。注意燃料燃燒用「甲烷」28，非「石化甲烷」30
GWP = {"二氧化碳": 1.0, "甲烷": 28.0, "石化甲烷": 30.0, "氧化亞氮": 265.0}

LHV = {  # 能源署 能源產品單位熱值表（淨熱值）
    "gasoline": (7520, "公升"),
    "diesel": (8629, "公升"),
    "lpg": (5958, "公升"),
    "natural_gas": (8107, "立方公尺"),
}
LHV_SRC = "能源署 能源產品單位熱值表(自113年起)"

FACTORS = {
    "TW-M-GASOLINE-OXI": FuelFactorInput(
        "gasoline", "車用汽油(移動-氧化觸媒)", 69300, 25, 8,
        source_ref="附表一-移動 第8列/第24列"),
    "TW-M-GASOLINE-UNC": FuelFactorInput(
        "gasoline", "車用汽油(移動-未控制)", 69300, 33, 3.2,
        source_ref="附表一-移動 第8列/第23列"),
    "TW-F-NG-S": FuelFactorInput(
        "natural_gas", "天然氣(固定)", 56100, 1, 0.1,
        source_ref="附表一-固定 第45列"),
    "TW-F-LPG-S": FuelFactorInput(
        "lpg", "液化石油氣(固定)", 63100, 1, 0.1,
        source_ref="附表一-固定 第19列"),
    "TW-F-DIESEL-S": FuelFactorInput(
        "diesel", "柴油(固定)", 74100, 3, 0.6,
        source_ref="附表一-固定 第17列"),
}

Y_START, Y_END = dt.date(2024, 1, 1), dt.date(2024, 12, 31)


def _derive(key):
    f = FACTORS[key]
    kcal, unit = LHV[f.factor_key]
    return derive_fuel_factor(f, kcal, unit, LHV_SRC, GWP)


# --------------------------------------------------------------------------
# 係數推導：對照 v5「燃料係數計算」分頁 U 欄
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key,expected", [
    ("TW-M-GASOLINE-OXI", 2.270679),
    ("TW-M-GASOLINE-UNC", 2.237683),
    ("TW-F-NG-S", 1.906018),
    ("TW-F-LPG-S", 1.575386),
    ("TW-F-DIESEL-S", 2.685856),
])
def test_derived_factor_matches_spreadsheet(key, expected):
    assert _derive(key).total_co2e_per_unit == pytest.approx(expected, abs=1e-6)


def test_ch4_uses_28_not_30():
    """平台公告：燃料燃燒採「甲烷」28。用成石化甲烷 30 會偏高。"""
    d = _derive("TW-M-GASOLINE-OXI")
    assert d.ch4_gwp == 28.0
    wrong = FuelFactorInput("gasoline", "x", 69300, 25, 8, ch4_gwp_gas="石化甲烷")
    d2 = derive_fuel_factor(wrong, 7520, "公升", LHV_SRC, GWP)
    assert d2.total_co2e_per_unit > d.total_co2e_per_unit


def test_missing_heating_value_fails_loudly():
    with pytest.raises(MissingHeatingValueError):
        derive_fuel_factor(FACTORS["TW-F-NG-S"], None, "立方公尺", "", GWP)


def test_unknown_unit_raises():
    with pytest.raises(UnitMismatchError):
        calculate_fuel(10, "加侖", _derive("TW-F-DIESEL-S"))


def test_kiloliter_converts():
    """表四範例以「公秉」計，1 公秉 = 1000 公升。"""
    a = calculate_fuel(1, "公秉", _derive("TW-F-DIESEL-S"))
    b = calculate_fuel(1000, "公升", _derive("TW-F-DIESEL-S"))
    assert a.total_co2e_kg == pytest.approx(b.total_co2e_kg)


# --------------------------------------------------------------------------
# 跨期分攤：對照 v5「活動數據登錄」第 1 筆
# --------------------------------------------------------------------------

def test_cross_year_allocation():
    a = allocate_period(8640, dt.date(2023, 12, 15), dt.date(2024, 2, 14),
                        Y_START, Y_END)
    assert a.total_days == 62
    assert a.days_in_year == 45
    assert a.ratio == pytest.approx(45 / 62)
    assert a.allocated_quantity == pytest.approx(6270.9677, abs=1e-4)
    assert a.is_cross_period is True
    assert a.quality_hint == "推估"


def test_fully_inside_year_not_allocated():
    a = allocate_period(7920, dt.date(2024, 2, 15), dt.date(2024, 4, 14),
                        Y_START, Y_END)
    assert a.ratio == 1.0
    assert a.is_cross_period is False
    assert a.quality_hint == "實測"


def test_period_entirely_outside_year():
    a = allocate_period(1000, dt.date(2023, 1, 1), dt.date(2023, 6, 30),
                        Y_START, Y_END)
    assert a.days_in_year == 0
    assert a.allocated_quantity == 0.0


# --------------------------------------------------------------------------
# 端到端：重現 v5 表八的總量
# --------------------------------------------------------------------------

RECORDS = [
    ("S01", "elec", 8640, "度", dt.date(2023, 12, 15), dt.date(2024, 2, 14)),
    ("S01", "elec", 7920, "度", dt.date(2024, 2, 15), dt.date(2024, 4, 14)),
    ("S02", "TW-M-GASOLINE-OXI", 45.2, "公升",
     dt.date(2024, 3, 5), dt.date(2024, 3, 5)),
    ("S02", "TW-M-GASOLINE-OXI", 49.2, "公升",
     dt.date(2024, 3, 20), dt.date(2024, 3, 20)),
    ("S04", "TW-F-NG-S", 310, "立方公尺",
     dt.date(2024, 1, 1), dt.date(2024, 3, 31)),
]
ELEC_113 = 0.474


def _run_all():
    scope1 = scope2 = 0.0
    for _, key, qty, unit, ps, pe in RECORDS:
        alloc = allocate_period(qty, ps, pe, Y_START, Y_END)
        if key == "elec":
            r = calculate_electricity(alloc.allocated_quantity, unit,
                                      ELEC_113, 113, "能源署")
            scope2 += r.total_co2e_kg
        else:
            r = calculate_fuel(alloc.allocated_quantity, unit, _derive(key))
            scope1 += r.total_co2e_kg
    return scope1, scope2


def test_totals_match_spreadsheet_table8():
    scope1, scope2 = _run_all()
    assert scope1 / 1000 == pytest.approx(0.805218, abs=1e-6)
    assert scope2 / 1000 == pytest.approx(6.726519, abs=1e-6)
    assert (scope1 + scope2) / 1000 == pytest.approx(7.531736, abs=1e-6)


def test_electricity_factor_not_double_counted():
    """電力係數已含 GWP，結果必須剛好等於 度數×係數。"""
    r = calculate_electricity(1000, "度", ELEC_113, 113)
    assert r.total_co2e_kg == pytest.approx(474.0)
    assert r.ch4_co2e_kg == 0.0 and r.n2o_co2e_kg == 0.0


# --------------------------------------------------------------------------
# 完整性檢查
# --------------------------------------------------------------------------

def test_completeness_flags_source_with_no_data():
    issues = check_completeness(
        [("S05", "備用發電機", True)], {}, Y_START, Y_END)
    assert len(issues) == 1 and issues[0].severity == "error"


def test_completeness_flags_missing_months():
    issues = check_completeness(
        [("S01", "台電電號", True)],
        {"S01": [(dt.date(2024, 1, 1), dt.date(2024, 3, 31))]},
        Y_START, Y_END)
    assert issues[0].severity == "warning"
    assert "4月" in issues[0].issue and "12月" in issues[0].issue


def test_completeness_passes_when_full_year_covered():
    issues = check_completeness(
        [("S01", "台電電號", True)],
        {"S01": [(dt.date(2024, 1, 1), dt.date(2024, 12, 31))]},
        Y_START, Y_END)
    assert issues == []
