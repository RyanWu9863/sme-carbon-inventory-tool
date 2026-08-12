"""
API 測試。

用 FastAPI 的 dependency_overrides 把 `get_db` 換成記憶體資料庫，所以不會碰到
專案目錄下的 `carbon.db`。

每個測試的 session 用 `join_transaction_mode="create_savepoint"` 綁在一個外層
transaction 上 —— API 內部會 `commit()`，這個模式讓那個 commit 只釋放 savepoint，
測試結束時整個外層一起回滾。不然第一個測試寫進去的東西會跟著後面所有測試跑。

兩個最重要的：

    test_summary_matches_spreadsheet   7.531736 走完 HTTP 還是同一個數字
    test_get_summary_does_not_write    GET 不改資料庫（連時間戳記都不能動）
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import app
from app.db import Base, get_db
from app.models import ActivityRecord, EmissionResult
from app.seed import DEFAULT_WORKBOOK, read_seed
from scripts.import_seed import import_all
from scripts.load_demo import load_demo, read_demo

TOTAL = 7.53173634180173


def _memory_engine():
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.fixture(scope="module")
def engine():
    eng = _memory_engine()
    Base.metadata.create_all(eng)
    setup = sessionmaker(bind=eng)()
    import_all(setup, read_seed(DEFAULT_WORKBOOK))
    load_demo(setup, read_demo(DEFAULT_WORKBOOK))
    setup.commit()
    setup.close()
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    conn = engine.connect()
    trans = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def empty_client():
    """空資料庫，用來確認 /health 講得出「種子資料沒匯入」。"""
    eng = _memory_engine()
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        session.close()
        eng.dispose()


# --------------------------------------------------------------------------
# 系統
# --------------------------------------------------------------------------

def test_health_reports_what_is_loaded(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["published_factors"] == 12
    assert body["material_codes"] == 6222
    assert body["factor_set"] == "環部授氣字第1139101231號"


def test_health_on_empty_database_says_what_to_run(empty_client):
    """
    空資料庫算出來的 0 跟真的 0 長得一樣。要能一眼分辨，而且要講出下一步指令。
    """
    body = empty_client.get("/health").json()
    assert body["status"] != "ok"
    assert "import_seed.py" in body["hint"]


# --------------------------------------------------------------------------
# 事業與清冊
# --------------------------------------------------------------------------

def test_list_orgs(client):
    orgs = client.get("/orgs").json()
    assert len(orgs) == 1
    assert orgs[0]["name"] == "示範小吃店"
    assert orgs[0]["year_start"] == "2024-01-01"      # 民國 113 → 西元 2024


def test_unknown_org_is_404(client):
    assert client.get("/orgs/999").status_code == 404


def test_sources_match_table3(client):
    sources = client.get("/orgs/1/sources").json()
    assert [s["source_no"] for s in sources] == ["S01", "S02", "S03", "S04", "S05"]

    by_no = {s["source_no"]: s for s in sources}
    assert by_no["S01"]["emission_type"] == "外購電力"
    assert by_no["S02"]["emission_type"] == "移動燃燒"
    assert by_no["S04"]["emission_type"] == "固定燃燒"
    assert by_no["S04"]["equipment_code"] == "B001"          # 燃氣台爐，不是 0020
    assert by_no["S03"]["record_count"] == 0                 # 清冊有，資料沒有


def test_sources_resolve_code_names(client):
    """
    代碼旁邊要附名稱。官方表三本來就是「代碼＋名稱」兩欄並列 —— 畫面上只顯示
    `9999` 沒有人看得懂，而看不懂的欄位使用者就會亂填。
    """
    by_no = {s["source_no"]: s for s in client.get("/orgs/1/sources").json()}

    assert by_no["S04"]["equipment_name"] == "燃氣台爐"
    assert by_no["S04"]["material_name"] == "天然氣"
    assert by_no["S01"]["equipment_name"] == "其他未歸類設施"
    assert by_no["S01"]["material_name"] == "外購台電電力"
    assert by_no["S02"]["process_name"] == "交通運輸活動"


def test_source_list_does_not_scale_queries_with_rows(client, db):
    """
    名稱解析是一次查完，不是每列各查一次。

    5 個排放源看不出差別，但這裡放著是為了擋住「在迴圈裡查資料庫」那種寫法 ——
    真的事業有幾十個排放源時，那會變成上百次查詢。
    """
    statements = []
    from sqlalchemy import event

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", record)
    try:
        client.get("/orgs/1/sources")
    finally:
        event.remove(db.bind, "before_cursor_execute", record)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 6, f"查了 {len(selects)} 次，應該是固定次數：\n" + "\n".join(selects)


# --------------------------------------------------------------------------
# 表八
# --------------------------------------------------------------------------

def test_summary_matches_spreadsheet(client):
    """7.531736 走完一趟 HTTP 之後還是同一個數字。"""
    body = client.post("/orgs/1/calculate").json()

    assert body["total_tco2e"] == pytest.approx(TOTAL, rel=1e-12)
    assert body["by_emission_type"]["固定燃燒"] == pytest.approx(0.590865519390102, rel=1e-12)
    assert body["by_emission_type"]["移動燃燒"] == pytest.approx(0.214352112734208, rel=1e-12)
    assert body["estimated_share"] == pytest.approx(0.40948806283192, rel=1e-12)


def test_summary_reports_completeness_issues(client):
    body = client.post("/orgs/1/calculate").json()
    assert body["has_errors"]

    errors = {i["source_no"] for i in body["issues"] if i["severity"] == "error"}
    assert errors == {"S03", "S05"}


def test_get_summary_does_not_write(client, db):
    """
    **GET 不能改資料庫。** 一開始把「算」跟「看」合在一個 GET 裡很方便，但那
    表示每次重新整理報表頁都在改 `calculated_at` —— 而那正是稽核要看的欄位。
    """
    client.post("/orgs/1/calculate")
    before = {
        r.id: (r.calculated_at, r.total_co2e_kg)
        for r in db.scalars(select(EmissionResult))
    }
    assert before

    for _ in range(3):
        assert client.get("/orgs/1/summary").status_code == 200

    after = {
        r.id: (r.calculated_at, r.total_co2e_kg)
        for r in db.scalars(select(EmissionResult))
    }
    assert after == before, "GET /summary 動到了計算結果"


def test_summary_before_calculating_reports_pending(client, db):
    """
    還沒算過的時候，總量是 0 —— 但那跟「真的排放 0」不一樣，
    要靠 uncalculated_count 分辨。
    """
    for result in db.scalars(select(EmissionResult)).all():
        db.delete(result)
    db.flush()

    body = client.get("/orgs/1/summary").json()
    assert body["total_tco2e"] == 0.0
    assert body["uncalculated_count"] == 5


def test_calculate_is_idempotent(client, db):
    """重算不該長出第二批結果。"""
    client.post("/orgs/1/calculate")
    first = db.scalar(select(func.count()).select_from(EmissionResult))

    second_body = client.post("/orgs/1/calculate").json()
    second = db.scalar(select(func.count()).select_from(EmissionResult))

    assert first == second == 5
    assert second_body["total_tco2e"] == pytest.approx(TOTAL, rel=1e-12)


# --------------------------------------------------------------------------
# 新增活動數據
# --------------------------------------------------------------------------

def _payload(**overrides) -> dict:
    body = {
        "period_start": "2024-05-01",
        "period_end": "2024-05-31",
        "raw_quantity": 100.0,
        "unit": "立方公尺",
        "data_quality": "實測",
        "evidence_type": "瓦斯帳單",
        "evidence_ref": "113年5月",
    }
    body.update(overrides)
    return body


def test_add_record_calculates_immediately(client):
    """
    新增就算，不等使用者按「產生報告」。錯誤要在他還看著表單時就講。
    """
    resp = client.post("/orgs/1/sources/S04/records", json=_payload())
    assert resp.status_code == 201

    body = resp.json()
    assert body["allocation_ratio"] == pytest.approx(1.0)
    assert body["total_co2e_kg"] == pytest.approx(100 * 1.9060178044842, rel=1e-12)
    assert "係數推導" in body["calc_trace"]


def test_estimated_without_basis_is_rejected_with_a_code(client):
    """
    422 + code。前端要靠 code 決定畫面 —— data_quality 該把使用者帶回那一筆
    活動數據，光看 422 做不到。
    """
    resp = client.post(
        "/orgs/1/sources/S04/records",
        json=_payload(data_quality="推估", estimation_basis=None),
    )
    assert resp.status_code == 422

    error = resp.json()["error"]
    assert error["code"] == "data_quality"
    assert "推估依據" in error["message"]


def test_failed_record_is_not_left_behind(client, db):
    """
    計算失敗時整筆不寫入。留下一筆算不出結果的活動數據，比擋下來糟 ——
    它會出現在清冊上，卻永遠不進總量。
    """
    before = db.scalar(select(func.count()).select_from(ActivityRecord))

    resp = client.post(
        "/orgs/1/sources/S04/records",
        json=_payload(data_quality="推估", estimation_basis="  "),
    )
    assert resp.status_code == 422

    after = db.scalar(select(func.count()).select_from(ActivityRecord))
    assert after == before, "計算失敗卻留下了活動數據"


def test_cross_year_bill_marked_measured_is_rejected(client):
    resp = client.post(
        "/orgs/1/sources/S04/records",
        json=_payload(period_start="2024-12-01", period_end="2025-01-31",
                      data_quality="實測"),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "data_quality"


def test_unknown_source_no_is_404(client):
    resp = client.post("/orgs/1/sources/S99/records", json=_payload())
    assert resp.status_code == 404


def test_period_end_before_start_is_rejected(client):
    resp = client.post(
        "/orgs/1/sources/S04/records",
        json=_payload(period_start="2024-05-31", period_end="2024-05-01"),
    )
    assert resp.status_code == 422


def test_negative_quantity_is_rejected(client):
    resp = client.post("/orgs/1/sources/S04/records", json=_payload(raw_quantity=-5))
    assert resp.status_code == 422


def test_added_record_shows_up_in_the_summary(client):
    client.post("/orgs/1/calculate")
    before = client.get("/orgs/1/summary").json()["total_tco2e"]

    client.post("/orgs/1/sources/S04/records", json=_payload())
    after = client.get("/orgs/1/summary").json()["total_tco2e"]

    assert after > before
    assert after - before == pytest.approx(100 * 1.9060178044842 / 1000, rel=1e-9)


# --------------------------------------------------------------------------
# 代碼表
# --------------------------------------------------------------------------

def test_material_code_search_by_name(client):
    body = client.get("/codes/material", params={"q": "車用汽油"}).json()
    assert body["total"] >= 1
    assert any(i["code"] == "170001" for i in body["items"])


def test_code_search_by_code_fragment(client):
    body = client.get("/codes/equipment", params={"q": "B00"}).json()
    codes = {i["code"] for i in body["items"]}
    assert {"B001", "B002"} <= codes


def test_search_reports_truncation(client):
    """
    6,222 筆不可能做成下拉選單。只回一個陣列的話，使用者搜到 50 筆會以為
    就是全部 —— 他要的可能在第 51 筆。
    """
    body = client.get("/codes/material", params={"limit": 5}).json()
    assert body["total"] == 6222
    assert body["returned"] == 5
    assert body["truncated"] is True


def test_search_without_query_still_reports_total(client):
    body = client.get("/codes/process").json()
    assert body["total"] == 1023


def test_unknown_code_table_is_404(client):
    resp = client.get("/codes/nope")
    assert resp.status_code == 404
    assert "material" in resp.json()["detail"]


# --------------------------------------------------------------------------
# 介面
# --------------------------------------------------------------------------

def test_root_serves_the_ui(client):
    """
    `/` 要回得出介面。單一 HTML 檔，沒有 build step —— 這個測試同時也是在確認
    檔案真的被打包在該在的位置（app/static/index.html），而不是只有我這台電腦有。
    """
    resp = client.get("/")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "碳盤查工具" in resp.text
    assert "<!DOCTYPE html>" in resp.text


def test_ui_is_self_contained(client):
    """
    介面不可依賴外部 CDN。

    這個工具的使用情境是中小企業的辦公室電腦，網路不一定通得到外面；而且
    demo 影片要能離線播完。CSS 與 JS 都必須內嵌。
    """
    html = client.get("/").text

    for pattern in ("src=\"http", "href=\"http", "//cdn.", "unpkg.com", "jsdelivr"):
        assert pattern not in html, f"介面引用了外部資源：{pattern}"


def test_ui_is_not_in_the_openapi_schema(client):
    """`/` 是給人看的，不該混進 API 文件。"""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/" not in paths


def test_factors_list_is_the_twelve_fuels(client):
    factors = client.get("/factors").json()
    assert len(factors) == 12
    assert {f["factor_key"] for f in factors} >= {"TW-F-NG-S", "TW-M-GASOLINE-OXI"}
    assert all(f["factor_set_version"] == "環部授氣字第1139101231號" for f in factors)

    # 電力不在這個清單裡：它係數已含 GWP、逐年公告，走完全不同的路徑。
    assert not any(f["factor_key"].startswith("TW-ELEC") for f in factors)
