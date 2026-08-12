"""
服務層測試 —— 這一組是整個專案的驗收。

最重要的一個是 `test_year_total_matches_spreadsheet_table8`：
**從資料庫算出 7.53173634180173 tCO2e**，與 v5 試算表表八相同。

試算表那邊是 Excel 公式，這邊是 Python 走 ORM 查係數、推導每單位係數、
跨期分攤再計算，兩條路徑完全獨立。同一個數字從兩條獨立路徑出來，才算得上
驗證過；只有一條路徑時，測試測的是「程式跟自己一致」。

資料庫建在記憶體，模組層級只建一次（匯 7,603 筆代碼表不便宜），每個測試
包在一個會回滾的 transaction 裡，所以彼此不干擾。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    ActivityRecord, DataQuality, EmissionResult, EmissionSource, EmissionType,
    HeatingValue, Organization,
)
from app.seed import DEFAULT_WORKBOOK, read_seed
from app.service import (
    DataQualityError, FactorNotFoundError, ServiceError, calculate_record,
    calculate_year, resolve_electricity_factor, resolve_fuel_factor,
)
from scripts.import_seed import import_all
from scripts.load_demo import load_demo, read_demo

# v5 試算表表八的值，逐位抄下來當對照。
TOTAL = 7.53173634180173
SCOPE1 = 0.80521763212431
SCOPE2 = 6.72651870967742
STATIONARY = 0.590865519390102
MOBILE = 0.102634698046464 + 0.111717414687744
ESTIMATED_SHARE = 0.40948806283192


@pytest.fixture(scope="module")
def demo_case():
    return read_demo(DEFAULT_WORKBOOK)


@pytest.fixture(scope="module")
def engine(demo_case):
    """記憶體資料庫，模組層級建一次。StaticPool 讓所有連線共用同一個 :memory:。"""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)

    setup = sessionmaker(bind=eng)()
    import_all(setup, read_seed(DEFAULT_WORKBOOK))
    load_demo(setup, demo_case)
    setup.commit()
    setup.close()

    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    """每個測試包在會回滾的 transaction 裡，改了什麼都不會影響下一個。"""
    conn = engine.connect()
    trans = conn.begin()
    db = Session(bind=conn)
    try:
        yield db
    finally:
        db.close()
        trans.rollback()
        conn.close()


@pytest.fixture
def org(session):
    return session.scalars(select(Organization)).one()


@pytest.fixture
def summary(session, org):
    return calculate_year(session, org)


# --------------------------------------------------------------------------
# 驗收：與試算表對數
# --------------------------------------------------------------------------

def test_year_total_matches_spreadsheet_table8(summary):
    """
    **本專案的驗收基準。** 從資料庫算出 7.531736 tCO2e。

    在此之前這個數字只有純函式算得出來（test_totals_match_spreadsheet_table8）。
    現在走的是完整路徑：ORM 查係數 → 推導每單位係數 → 跨期分攤 → 計算 →
    彙總。中間任何一段接錯，這個數字就不會對。
    """
    assert summary.total_tco2e == pytest.approx(TOTAL, rel=1e-12)


def test_scope_split_matches_spreadsheet(summary):
    assert summary.scope1_tco2e == pytest.approx(SCOPE1, rel=1e-12)
    assert summary.scope2_tco2e == pytest.approx(SCOPE2, rel=1e-12)


def test_stationary_and_mobile_are_split(summary):
    """
    **試算表做不到的事。** 表八把範疇一全掛在「固定燃燒」，B11 自己註明
    「本試算表未依設備別拆分固定/移動，程式版應由表三排放源清冊的…」。

    拆分依據是表三的「排放型式」，不是燃料種類 —— 柴油在這個案例裡同時是
    固定燃燒（S05 備用發電機）與移動燃燒（S02 公務車），依燃料猜一定猜錯。
    """
    assert summary.by_emission_type["固定燃燒"] == pytest.approx(STATIONARY, rel=1e-12)
    assert summary.by_emission_type["移動燃燒"] == pytest.approx(MOBILE, rel=1e-12)
    assert summary.by_emission_type["外購電力"] == pytest.approx(SCOPE2, rel=1e-12)

    total = sum(summary.by_emission_type.values())
    assert total == pytest.approx(TOTAL, rel=1e-12), "拆分後加總必須等於總量"


def test_every_record_matches_the_spreadsheet(session, org, demo_case, summary):
    """
    逐筆對照試算表 V 欄，五筆都要一致。

    只驗總量不夠 —— 兩筆一多一少剛好抵銷，總量還是對的。
    """
    expected = {r.evidence_ref: r.spreadsheet_tco2e for r in demo_case.records}
    checked = 0

    for result in summary.results:
        record = session.get(ActivityRecord, result.record_id)
        want = expected[record.evidence_ref]
        assert want is not None
        assert result.total_co2e_kg / 1000.0 == pytest.approx(want, rel=1e-12), (
            f"{record.evidence_ref} 與試算表不一致"
        )
        checked += 1

    assert checked == 5


def test_estimated_share_matches_spreadsheet(summary):
    """表八「資料品質揭露」：實測 3 筆、推估 2 筆、推估排放量占比 40.9488%。"""
    assert summary.measured_count == 3
    assert summary.estimated_count == 2
    assert summary.estimated_share == pytest.approx(ESTIMATED_SHARE, rel=1e-12)


def test_gas_split_is_real_not_all_co2(summary):
    """
    另一件比試算表準的事：試算表表八把 100% 都算成 CO2，但燃料燃燒確實會排
    CH4 與 N2O（每單位係數裡就含它們）。這裡照實拆開。

    總量不受影響 —— 拆的是同一筆錢怎麼分。
    """
    assert summary.by_gas["CH4"] > 0
    assert summary.by_gas["N2O"] > 0
    assert sum(summary.by_gas.values()) == pytest.approx(TOTAL, rel=1e-12)


# --------------------------------------------------------------------------
# 跨期分攤
# --------------------------------------------------------------------------

def test_cross_period_bill_is_allocated_by_days(session, org, summary):
    """
    第 1 筆是雙月期電費單 2023-12-15 ~ 2024-02-14，共 62 天，落在 113 年度
    （2024 全年）的只有 45 天。試算表 M7 = 45/62 ≒ 0.7258。
    """
    record = session.scalars(
        select(ActivityRecord).where(ActivityRecord.evidence_ref == "113年1-2月期")
    ).one()

    assert record.allocation_ratio == pytest.approx(45 / 62, rel=1e-12)
    assert record.allocated_quantity == pytest.approx(8640 * 45 / 62, rel=1e-12)
    assert "跨期分攤" in record.result.calc_trace


def test_fully_inside_year_is_not_allocated(session, summary):
    record = session.scalars(
        select(ActivityRecord).where(ActivityRecord.evidence_ref == "113年3-4月期")
    ).one()

    assert record.allocation_ratio == pytest.approx(1.0)
    assert record.allocated_quantity == pytest.approx(7920.0)


# --------------------------------------------------------------------------
# 資料品質：服務層強制
# --------------------------------------------------------------------------

def test_estimated_without_basis_is_refused(session, org):
    """
    models.py 註明 estimation_basis「推估時必填，由服務層強制」——
    資料庫本身沒有這個約束，所以要有測試證明服務層真的擋。
    """
    record = session.scalars(
        select(ActivityRecord).where(ActivityRecord.evidence_ref == "AB-12345679")
    ).one()
    assert record.data_quality == DataQuality.ESTIMATED

    record.estimation_basis = "   "          # 空白也算沒填

    with pytest.raises(DataQualityError, match="推估依據"):
        calculate_record(session, record, org)


def test_cross_period_marked_measured_is_refused(session, org):
    """
    跨年帳單分攤後的數字本質上是推估。標成「實測」是實質錯誤，不是格式問題。
    """
    record = session.scalars(
        select(ActivityRecord).where(ActivityRecord.evidence_ref == "113年1-2月期")
    ).one()
    record.data_quality = DataQuality.MEASURED

    with pytest.raises(DataQualityError, match="跨出盤查年度"):
        calculate_record(session, record, org)


# --------------------------------------------------------------------------
# 完整性檢查
# --------------------------------------------------------------------------

def test_sources_without_any_data_are_errors(summary):
    """
    S03 外送機車隊與 S05 備用發電機列在清冊上卻沒有任何活動數據。

    這是最陰險的漏洞 —— 少一個排放源，總量少一截，卻不會有任何錯誤訊息。
    示範資料刻意保留這個狀態，正好證明檢查有在運作。
    """
    errors = {i.source_no for i in summary.issues if i.severity == "error"}
    assert errors == {"S03", "S05"}
    assert summary.has_errors


def test_partial_year_coverage_is_warned(summary):
    """示範資料只涵蓋 1~4 月，有資料的三個排放源都該被指出缺哪些月份。"""
    warnings = {i.source_no: i.issue for i in summary.issues if i.severity == "warning"}
    assert set(warnings) == {"S01", "S02", "S04"}
    assert "12月" in warnings["S01"]


# --------------------------------------------------------------------------
# 稽核快照
# --------------------------------------------------------------------------

def test_fuel_result_pins_down_the_factor_version(session, summary):
    """
    公告改版後歷史報告要能重現，靠的就是這些快照欄位。沒釘住的話，改版當天
    所有舊報告的數字就再也解釋不了了。
    """
    record = session.scalars(
        select(ActivityRecord).where(ActivityRecord.evidence_ref == "113年Q1")
    ).one()
    result = record.result

    assert result.heating_value_used == pytest.approx(8107.0)
    assert "能源署" in result.heating_value_source
    assert result.factor_set_version == "環部授氣字第1139101231號"
    assert result.ch4_gwp_used == 28          # 不是石化甲烷的 30
    assert result.n2o_gwp_used == 265
    assert result.published_factor_id is not None
    assert result.calc_trace


def test_electricity_result_records_the_factor_not_gwp(session, summary):
    """
    電力係數已是合併 CO2e，不可再乘 GWP —— models.py 說這是最常見的錯誤。
    所以電力的結果不該有 GWP 快照，該有的是當年度的電力係數。
    """
    record = session.scalars(
        select(ActivityRecord).where(ActivityRecord.evidence_ref == "113年3-4月期")
    ).one()
    result = record.result

    assert result.electricity_factor_used == pytest.approx(0.474)
    assert result.ch4_gwp_used is None
    assert result.n2o_gwp_used is None
    assert result.heating_value_used is None
    assert result.total_co2e_kg == pytest.approx(7920 * 0.474, rel=1e-12)


def test_recalculating_updates_instead_of_duplicating(session, org):
    """報告重跑要覆蓋舊結果，不是長出第二筆。"""
    calculate_year(session, org)
    session.flush()
    first = session.scalar(select(func.count()).select_from(EmissionResult))

    calculate_year(session, org)
    session.flush()
    second = session.scalar(select(func.count()).select_from(EmissionResult))

    assert first == second == 5


# --------------------------------------------------------------------------
# 查係數
# --------------------------------------------------------------------------

def test_org_heating_value_overrides_the_default(session, org):
    """
    熱值是唯一「使用者可以合法覆寫官方值」的資料，環境部原意就是由事業填入
    自身燃料實際熱值。有自填值時必須優先採用，否則覆寫功能形同虛設。
    """
    default = resolve_fuel_factor(session, "TW-F-NG-S", org.id)
    assert default.heating.kcal_per_unit == pytest.approx(8107.0)

    session.add(HeatingValue(
        material_code="050002", factor_key="050002", display_name="天然氣",
        unit="立方公尺", kcal_per_unit=8755.0, source="瓦斯公司當期熱值公告",
        org_id=org.id, is_user_override=True,
    ))
    session.flush()

    overridden = resolve_fuel_factor(session, "TW-F-NG-S", org.id)
    assert overridden.heating.kcal_per_unit == pytest.approx(8755.0)
    assert overridden.derived.total_co2e_per_unit > default.derived.total_co2e_per_unit


def test_unknown_factor_key_says_where_to_look(session):
    with pytest.raises(FactorNotFoundError, match="TW-F-NOPE"):
        resolve_fuel_factor(session, "TW-F-NOPE")


def test_unpublished_electricity_year_says_so(session):
    """
    114 年度尚未公告。要講「尚未公告」而不是「查無係數」—— 使用者能做的事
    完全不同：前者是等，後者是去修資料庫。
    """
    with pytest.raises(FactorNotFoundError, match="尚未公告"):
        resolve_electricity_factor(session, 114)


def test_unsupported_emission_type_is_refused(session, org):
    """
    製程、逸散、外購蒸汽目前不支援。要報錯而不是算成 0 —— 算成 0 會讓報告
    少一截卻沒有任何提示，正是這個工具最該防的那種錯。
    """
    source = session.scalars(
        select(EmissionSource).where(EmissionSource.source_no == "S04")).one()
    source.emission_type = EmissionType.PROCESS
    session.flush()

    record = session.scalars(
        select(ActivityRecord).where(ActivityRecord.source_id == source.id)).one()

    with pytest.raises(ServiceError, match="製程"):
        calculate_record(session, record, org)


def test_period_entirely_outside_the_year_contributes_nothing(session, org):
    """完全落在盤查年度外的單據分攤為 0，但仍要留下紀錄，不是丟掉。"""
    record = session.scalars(
        select(ActivityRecord).where(ActivityRecord.evidence_ref == "113年Q1")
    ).one()
    record.period_start = dt.date(2022, 1, 1)
    record.period_end = dt.date(2022, 3, 31)
    record.data_quality = DataQuality.ESTIMATED
    record.estimation_basis = "測試用：整段期間都在盤查年度外"

    result = calculate_record(session, record, org)

    assert record.allocation_ratio == 0.0
    assert result.total_co2e_kg == 0.0
