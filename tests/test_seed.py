"""
種子資料驗收：從試算表讀出來的數字，用計算引擎重算一次，必須與
試算表自己算的結果一致。

這件事的意義：試算表的 U 欄是 Excel 公式算的，程式的每單位係數是
Python 算的，兩條路徑完全獨立。兩邊對得上，才能說「程式化」沒有
在過程中改變任何數字。

test_calculator.py 涵蓋 5 個燃料，是手抄進測試檔的。這裡涵蓋全部
12 個，而且不是手抄 —— 新增燃料時這裡自動跟著測，不必改測試。
"""

import pytest

from app.calculator import KCAL_TO_TJ, FuelFactorInput, derive_fuel_factor
from app.seed import DEFAULT_WORKBOOK, SeedFormatError, gwp_lookup, read_seed


@pytest.fixture(scope="module")
def seed():
    return read_seed(DEFAULT_WORKBOOK)


# --------------------------------------------------------------------------
# 核心：程式重算 vs 試算表公式
# --------------------------------------------------------------------------

def test_every_fuel_matches_the_spreadsheet(seed):
    """12 個燃料逐一比對。差在小數第 9 位以後才算相同。"""
    gwp = gwp_lookup(seed)
    mismatches = []
    for fuel in seed.fuels:
        derived = derive_fuel_factor(
            FuelFactorInput(
                factor_key=fuel.activity_key,
                display_name=fuel.display_name,
                co2_kg_per_tj=fuel.co2_kg_per_tj,
                ch4_kg_per_tj=fuel.ch4_kg_per_tj,
                n2o_kg_per_tj=fuel.n2o_kg_per_tj,
                ch4_gwp_gas=fuel.ch4_gwp_gas,
                source_ref=fuel.source_ref,
            ),
            fuel.heating_value_kcal,
            fuel.unit,
            fuel.heating_value_source,
            gwp,
        )
        if derived.total_co2e_per_unit != pytest.approx(
            fuel.spreadsheet_total, abs=1e-9
        ):
            mismatches.append(
                f"{fuel.factor_code}: 程式 {derived.total_co2e_per_unit!r}"
                f" vs 試算表 {fuel.spreadsheet_total!r}"
            )

    assert not mismatches, "程式與試算表不一致：\n" + "\n".join(mismatches)


def test_all_twelve_fuels_are_read(seed):
    """少讀一列不會有任何錯誤訊息，只會少一個燃料可選。"""
    assert len(seed.fuels) == 12
    assert len({f.factor_code for f in seed.fuels}) == 12, "係數編號有重複"


def test_conversion_constant_agrees_with_the_code(seed):
    """1 kcal = 4.1868E-9 TJ 在試算表和程式各寫了一份，必須相同。"""
    assert seed.kcal_to_tj == pytest.approx(KCAL_TO_TJ, rel=1e-12)


# --------------------------------------------------------------------------
# GWP：28 而非 30 這件事，種子資料端也要守住
# --------------------------------------------------------------------------

def test_fuel_combustion_uses_methane_28(seed):
    gwp = gwp_lookup(seed)
    assert gwp["甲烷"] == 28.0
    assert gwp["石化甲烷"] == 30.0
    assert seed.gwp_standard == "AR5"
    # 所有燃料列都必須指向「甲烷」，指到石化甲烷代表 J 欄被改過
    assert {f.ch4_gwp_gas for f in seed.fuels} == {"甲烷"}


def test_gwp_table_has_the_four_gases(seed):
    assert {row.gas_name for row in seed.gwp} == {
        "二氧化碳", "甲烷", "石化甲烷", "氧化亞氮"
    }


# --------------------------------------------------------------------------
# 電力係數
# --------------------------------------------------------------------------

def test_electricity_factors_are_per_year(seed):
    by_year = {row.year_roc: row.kgco2e_per_kwh for row in seed.electricity}
    assert by_year[113] == 0.474
    assert by_year[112] == 0.494


def test_unpublished_electricity_year_is_reported_not_silently_dropped(seed):
    """114 年度尚未公告。跳過它是對的，但不能無聲無息。"""
    assert 114 in seed.electricity_pending_years
    assert 114 not in {row.year_roc for row in seed.electricity}


# --------------------------------------------------------------------------
# 代碼表
# --------------------------------------------------------------------------

def test_code_blocks_are_read_separately(seed):
    """三個代碼區塊並排在同一張表，讀混了就會對應到錯的代碼。"""
    process = {row.code for row in seed.process_codes}
    equipment = {row.code for row in seed.equipment_codes}
    material = {row.code for row in seed.material_codes}

    assert "G20900" in process and "000000" in process
    assert "0200" in equipment and "9999" in equipment
    assert "170001" in material and "GG3500" in material
    assert not (process & equipment) and not (equipment & material)


def test_material_codes_cover_every_fuel(seed):
    """燃料的原燃物料代碼若不在代碼表裡，之後建外鍵會失敗。"""
    known = {row.code for row in seed.material_codes}
    missing = {f.material_code for f in seed.fuels} - known
    assert not missing, f"代碼表缺少：{sorted(missing)}"


# --------------------------------------------------------------------------
# 版面異常要明確失敗
# --------------------------------------------------------------------------

def test_missing_workbook_says_what_to_do():
    with pytest.raises(FileNotFoundError, match="碳盤查試算表_v5"):
        read_seed("does_not_exist.xlsx")


def test_moved_column_is_caught(tmp_path):
    """欄位被搬動時，讀出來的種子資料看起來完全正常，所以必須擋在門口。"""
    import openpyxl

    wb = openpyxl.load_workbook(DEFAULT_WORKBOOK)
    wb["燃料係數計算"]["G6"] = "熱值"      # 假裝有人把欄位對調了
    broken = tmp_path / "broken.xlsx"
    wb.save(broken)

    with pytest.raises(SeedFormatError, match="欄位順序"):
        read_seed(broken)
