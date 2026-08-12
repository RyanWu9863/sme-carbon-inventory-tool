"""
種子資料匯入的測試。

這一組會建立資料庫，但**建在記憶體裡**（sqlite:///:memory:），不碰
專案目錄下的 carbon.db。所以跑測試不會蓋掉你手上正在看的那個檔，
也不會因為 carbon.db 的殘留狀態讓測試時綠時紅。

最重要的兩個：

    test_running_twice_changes_nothing   可重複執行，這是 upsert 的全部意義
    test_factors_survive_the_round_trip  12 個係數經資料庫往返後仍與試算表一致
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.calculator import FuelFactorInput, derive_fuel_factor
from app.db import Base
from app.models import (
    ElectricityFactor, EquipmentCode, FactorSet, GwpValue, HeatingValue,
    MaterialCode, ProcessCode, PublishedFactor,
)
from app.seed import DEFAULT_WORKBOOK, read_seed
from scripts.import_seed import (
    SeedImportError, collapse_heating_values, import_all, read_code_csv,
)

EXPECTED_ROWS = {
    ProcessCode: 1023,
    EquipmentCode: 358,
    MaterialCode: 6222,
    GwpValue: 4,
    FactorSet: 1,
    PublishedFactor: 12,
    HeatingValue: 6,
    ElectricityFactor: 2,
}


@pytest.fixture(scope="module")
def data():
    return read_seed(DEFAULT_WORKBOOK)


@pytest.fixture
def session():
    """每個測試一個乾淨的記憶體資料庫。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def imported(session, data):
    import_all(session, data)
    session.commit()
    return session


# --------------------------------------------------------------------------
# 熱值去重（純函式，不碰資料庫）
# --------------------------------------------------------------------------

def test_shared_material_code_collapses_to_one_heating_value(data):
    """
    12 個燃料列共用 6 個原燃物料代碼 —— 汽油的固定燃燒與三種車輛技術別
    都是 170001，熱值都是 7,520 kcal/公升。熱值是燃料的物理性質，跟用途
    無關，資料庫裡只該有一筆。
    """
    collapsed = collapse_heating_values(data.fuels)

    assert len(data.fuels) == 12
    assert len(collapsed) == 6
    assert len({f.material_code for f in collapsed}) == 6


def test_conflicting_heating_values_are_refused(data):
    """
    同一代碼卻有兩組熱值，代表試算表自己打架。任選一筆會讓某些燃料的
    係數整組偏掉，而且不會有任何錯誤訊息。
    """
    gasoline = next(f for f in data.fuels if f.material_code == "170001")
    tampered = [
        gasoline,
        dataclasses.replace(gasoline, factor_code="FAKE", heating_value_kcal=9999.0),
    ]

    with pytest.raises(SeedImportError) as exc:
        collapse_heating_values(tampered)

    assert "170001" in str(exc.value)
    assert "9999" in str(exc.value)


# --------------------------------------------------------------------------
# 代碼表 CSV
# --------------------------------------------------------------------------

def test_missing_code_csv_says_how_to_regenerate():
    with pytest.raises(SeedImportError) as exc:
        read_code_csv("no_such_table")
    assert "extract_codes.py" in str(exc.value)


# --------------------------------------------------------------------------
# 匯入結果
# --------------------------------------------------------------------------

@pytest.mark.parametrize("model, expected", EXPECTED_ROWS.items())
def test_row_counts(imported, model, expected):
    assert imported.scalar(select(func.count()).select_from(model)) == expected


def test_running_twice_changes_nothing(session, data):
    """
    可重複執行是 upsert 的全部意義。跑兩次筆數要相同，而且第二次的
    tally 必須全部落在 unchanged —— 只驗筆數不夠，「刪掉再新增一筆」
    筆數也一樣。
    """
    import_all(session, data)
    session.commit()
    first = {m: session.scalar(select(func.count()).select_from(m)) for m in EXPECTED_ROWS}

    tallies = import_all(session, data)
    session.commit()
    second = {m: session.scalar(select(func.count()).select_from(m)) for m in EXPECTED_ROWS}

    assert first == second
    for tally in tallies:
        assert (tally.inserted, tally.updated) == (0, 0), (
            f"「{tally.label}」第二次跑還在動：新增 {tally.inserted}、更新 {tally.updated}"
        )


def test_no_orphan_foreign_keys(imported):
    """
    係數指到的原燃物料代碼必須真的存在。

    匯入順序反了（係數先於代碼表）在 SQLite 預設不會報錯 —— SQLite 預設
    不強制外鍵，你會得到一個外鍵指向空氣的資料庫，換到 PostgreSQL 才炸。
    """
    codes = set(imported.scalars(select(MaterialCode.code)))

    orphan_factors = [
        p.factor_key for p in imported.scalars(select(PublishedFactor))
        if p.material_code not in codes
    ]
    orphan_heating = [
        h.material_code for h in imported.scalars(select(HeatingValue))
        if h.material_code not in codes
    ]

    assert not orphan_factors, f"這些係數的原燃物料代碼不存在：{orphan_factors}"
    assert not orphan_heating, f"這些熱值的原燃物料代碼不存在：{orphan_heating}"


def test_factor_set_records_the_announcement(imported, data):
    """
    FactorSet 要能回答「這批係數是哪一份公告」，否則 models.py 的
    「計算結果做快照」就落空了。
    """
    fs = imported.scalars(select(FactorSet)).one()

    assert fs.doc_no == "環部授氣字第1139101231號"
    assert fs.publish_date.isoformat() == "2024-02-05"
    assert fs.version == fs.doc_no      # 公告文號即版本
    assert fs.gwp_standard == data.gwp_standard


def test_factors_survive_the_round_trip(imported, data):
    """
    **最重要的一個。** 從資料庫的值重算 12 個係數，必須與試算表 U 欄一致。

    匯入時欄位對錯（CO2 寫進 CH4 欄之類）不會有任何錯誤訊息，資料庫看起來
    完全正常，只是每個數字都錯。這個測試是唯一擋得住的東西。
    """
    spreadsheet = {f.factor_code: f.spreadsheet_total for f in data.fuels}
    gwp = {g.gas_name: g.gwp100 for g in imported.scalars(select(GwpValue))}
    heating = {h.material_code: h for h in imported.scalars(select(HeatingValue))}

    checked = 0
    for factor in imported.scalars(select(PublishedFactor)):
        hv = heating[factor.material_code]
        derived = derive_fuel_factor(
            FuelFactorInput(
                factor_key=factor.factor_key,
                display_name=factor.display_name,
                co2_kg_per_tj=factor.co2_kg_per_tj,
                ch4_kg_per_tj=factor.ch4_kg_per_tj,
                n2o_kg_per_tj=factor.n2o_kg_per_tj,
                ch4_gwp_gas=factor.ch4_gwp_gas,
            ),
            heating_value_kcal=hv.kcal_per_unit,
            heating_value_unit=hv.unit,
            heating_value_source=hv.source,
            gwp=gwp,
        )
        assert derived.total_co2e_per_unit == pytest.approx(
            spreadsheet[factor.factor_key], rel=1e-12
        ), f"{factor.factor_key} 經資料庫往返後與試算表不一致"
        checked += 1

    assert checked == 12


def test_fuel_combustion_still_uses_methane_28(imported):
    """
    資料庫端也要守住 28。這件事在計算引擎、種子資料讀取、資料庫三個地方
    各測一次，因為它在三個地方都可能被改回 30。
    """
    gwp = {g.gas_name: g.gwp100 for g in imported.scalars(select(GwpValue))}
    assert gwp["甲烷"] == 28

    for factor in imported.scalars(select(PublishedFactor)):
        assert factor.ch4_gwp_gas == "甲烷", (
            f"{factor.factor_key} 用了「{factor.ch4_gwp_gas}」，"
            f"燃料燃燒應一律採甲烷(28)"
        )


def test_unpublished_electricity_year_is_not_in_the_database(imported, data):
    """114 年度尚未公告，不應該憑空出現一筆。"""
    years = {e.year_roc for e in imported.scalars(select(ElectricityFactor))}

    assert years == {112, 113}
    assert 114 in data.electricity_pending_years
    assert 114 not in years


def test_user_override_heating_values_are_not_touched(session, data):
    """
    事業自行提供的熱值（org_id 有值）是使用者資料，匯入絕不能蓋掉它。
    models.py 明講熱值是唯一「使用者可以合法覆寫官方值」的資料。
    """
    import_all(session, data)
    session.commit()

    session.add(HeatingValue(
        material_code="170001", factor_key="170001", display_name="車用汽油",
        unit="公升", kcal_per_unit=7000.0, source="自行檢測", org_id=1,
        is_user_override=True,
    ))
    session.commit()

    import_all(session, data)
    session.commit()

    override = session.scalars(
        select(HeatingValue).where(HeatingValue.org_id == 1)
    ).one()
    assert override.kcal_per_unit == 7000.0
    assert override.is_user_override is True

    default = session.scalars(
        select(HeatingValue).where(
            HeatingValue.material_code == "170001", HeatingValue.org_id.is_(None))
    ).one()
    assert default.kcal_per_unit == 7520.0
