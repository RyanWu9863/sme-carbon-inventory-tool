"""
代碼表 CSV 的驗收測試。

**這些測試都不需要官方 .ods。** 去重邏輯用自編假資料測，其餘測已經進
版控的 CSV。所以任何一台 clone 這個 repo 的電腦都跑得起來 —— 這正是把
代碼表抽成 CSV 的目的：抽完之後，916 KB 的官方原檔就不必再跟著跑。

筆數在這裡寫死，是刻意的。extract_codes.py 不寫死任何筆數（官方改版時
它照樣能跑），由這裡當閘門：數字一變，測試就紅，逼你確認是官方真的改版
還是解析壞了，而不是讓它靜靜地變。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.seed import DEFAULT_WORKBOOK, read_seed
from scripts.extract_codes import (
    CSV_HEADER,
    ExtractError,
    OfficialCode,
    dedupe,
)

CODES_DIR = Path(__file__).resolve().parents[1] / "data" / "codes"

# 官方 2026/6/1 修正版，去重後的筆數。
EXPECTED_COUNTS = {
    "process_code": 1023,
    "equipment_code": 358,
    "material_code": 6222,
}


def load(stem: str) -> list[dict[str, str]]:
    """讀一個代碼表 CSV。encoding 用 utf-8-sig，因為輸出帶 BOM（供 Excel 開啟）。"""
    path = CODES_DIR / f"{stem}.csv"
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------
# 去重邏輯（自編假資料，不碰檔案）
# --------------------------------------------------------------------------

def test_identical_repeat_is_dropped():
    """官方表單把同一段複製兩次 —— 代碼與名稱都相同，刪掉後面那筆。"""
    rows = [
        OfficialCode(1, "A001", "汽油"),
        OfficialCode(2, "A002", "柴油"),
        OfficialCode(3, "A001", "汽油"),
    ]
    kept, removed = dedupe(rows, "測試表")

    assert [r.code for r in kept] == ["A001", "A002"]
    assert len(removed) == 1
    assert removed[0].row.official_seq == 3
    assert removed[0].duplicate_of.official_seq == 1


def test_same_code_different_name_refuses_to_choose():
    """
    同代碼不同名稱不是重複貼上，是真的衝突。

    程式替你挑一個，等於把一個需要決策的問題變成一個看不見的問題。
    """
    rows = [
        OfficialCode(1, "A001", "汽油"),
        OfficialCode(2, "A001", "車用汽油"),
    ]
    with pytest.raises(ExtractError) as exc:
        dedupe(rows, "測試表")

    message = str(exc.value)
    assert "A001" in message
    assert "汽油" in message and "車用汽油" in message


def test_official_order_is_preserved():
    """
    保留官方原始順序，不照代碼排序。

    順序一旦被打亂，就沒辦法跟未來新版的官方表做有意義的 diff。
    """
    rows = [
        OfficialCode(1, "Z999", "最後一個"),
        OfficialCode(2, "A001", "第一個"),
        OfficialCode(3, "M500", "中間"),
    ]
    kept, _ = dedupe(rows, "測試表")
    assert [r.code for r in kept] == ["Z999", "A001", "M500"]


# --------------------------------------------------------------------------
# 已進版控的 CSV
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stem, expected", EXPECTED_COUNTS.items())
def test_row_count_matches_the_official_form(stem, expected):
    assert len(load(stem)) == expected


@pytest.mark.parametrize("stem", EXPECTED_COUNTS)
def test_header_and_fields_are_intact(stem):
    rows = load(stem)
    assert list(rows[0]) == CSV_HEADER
    for row in rows:
        assert row["code"], f"{stem} 序號 {row['official_seq']} 代碼是空的"
        assert row["name"], f"{stem} 序號 {row['official_seq']} 名稱是空的"


@pytest.mark.parametrize("stem", EXPECTED_COUNTS)
def test_codes_are_unique(stem):
    """代碼是 models.py 裡的主鍵，重複會直接擋住匯入。"""
    codes = [row["code"] for row in load(stem)]
    assert len(codes) == len(set(codes))


# --------------------------------------------------------------------------
# 與 v5 試算表的交叉比對
# --------------------------------------------------------------------------

# v5 試算表手挑了 23 筆代碼進「代碼表」分頁，係數就是靠它們連到
# material_code。官方全量匯入後這兩份必須完全一致，否則外鍵會斷。
_V5_VS_OFFICIAL = [
    ("process_codes", "process_code"),
    ("equipment_codes", "equipment_code"),
    ("material_codes", "material_code"),
]


@pytest.mark.parametrize("v5_attr, official_stem", _V5_VS_OFFICIAL)
def test_v5_codes_agree_with_the_official_table(v5_attr, official_stem):
    """
    v5 試算表裡的代碼，官方表必須有，而且名稱要逐字相同。

    v5 的代碼表是手挑的子集（6／9／8 筆），抄錯一個字不會有任何錯誤訊息，
    只會讓係數指向一個不存在或名稱對不上的代碼。
    """
    official = {row["code"]: row["name"] for row in load(official_stem)}
    v5_rows = getattr(read_seed(DEFAULT_WORKBOOK), v5_attr)
    assert v5_rows, f"v5 試算表的 {v5_attr} 是空的"

    for row in v5_rows:
        assert row.code in official, (
            f"v5 用了代碼 {row.code}（{row.name}），"
            f"但官方 {official_stem} 查無此代碼"
        )
        assert official[row.code] == row.name, (
            f"代碼 {row.code} 名稱不一致："
            f"v5=「{row.name}」官方=「{official[row.code]}」"
        )


def test_every_fuel_material_code_is_in_the_official_table():
    """
    12 個燃料的原燃物料代碼都要在官方表裡。

    PublishedFactor.material_code 是指向 material_code 的外鍵，
    對不到就是匯入時才會炸的錯誤。
    """
    official = {row["code"] for row in load("material_code")}
    fuels = read_seed(DEFAULT_WORKBOOK).fuels

    missing = {f.material_code for f in fuels} - official
    assert not missing, f"這些燃料代碼在官方表裡找不到：{sorted(missing)}"
