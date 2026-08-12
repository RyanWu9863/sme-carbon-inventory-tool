"""
把種子資料寫進 carbon.db。

    python scripts/import_seed.py

兩個來源，分工不同：

    data/codes/*.csv        官方代碼表 7,603 筆（附表五～七），已進版控
    碳盤查試算表_v5.xlsx    係數／熱值／電力／GWP，所有數字的基準

**匯入順序不能反。** `PublishedFactor.material_code` 與
`HeatingValue.material_code` 都是指向 `MaterialCode` 的外鍵，代碼表沒先
進去，係數就沒有東西可以指。

**可重複執行。** 依自然鍵 upsert，跑兩次結果相同。刻意不用「先 DELETE
再 INSERT」—— 那會讓已經算好的 EmissionResult 外鍵指向消失的列，而且
每跑一次 id 就換一輪，快照就不再是快照了。

讀取邏輯全部在 app/seed.py 與 scripts/extract_codes.py，這裡只負責寫入。
分開的理由跟那兩支一樣：會失敗的邏輯留在純函式裡，才測得動。
"""

from __future__ import annotations

import csv
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# 直接執行 `python scripts/import_seed.py` 時，Python 放進 sys.path 的是
# scripts/ 而不是專案根目錄，於是 `import app` 會失敗。README 已經把
# 「No module named 'app'」列為最常見的錯誤，不該再多一個入口。
# 補上根目錄，讓這支跟 extract_codes.py 用同樣的方式執行。
# （`python -m scripts.import_seed` 本來就可以，這行只是讓兩種都行。）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select                                    # noqa: E402
from sqlalchemy.orm import Session                               # noqa: E402

from app.db import SessionLocal, init_db                         # noqa: E402
from app.models import (                                         # noqa: E402
    ElectricityFactor, EquipmentCode, FactorSet, FuelUsage, GwpValue,
    HeatingValue, MaterialCode, ProcessCode, PublishedFactor,
)
from app.seed import (                                           # noqa: E402
    DEFAULT_WORKBOOK, FuelRow, SeedData, read_seed,
)

ROOT = Path(__file__).resolve().parents[1]
CODES_DIR = ROOT / "data" / "codes"

# 檔名 → model。extract_codes.py 刻意讓 CSV 檔名等於 __tablename__。
CODE_TABLES = [
    ("process_code", ProcessCode, "製程"),
    ("equipment_code", EquipmentCode, "設備"),
    ("material_code", MaterialCode, "原(燃)物料"),
]


class SeedImportError(RuntimeError):
    """種子資料本身有問題。寧可不匯入，也不要匯入一半。"""


def _pad(text: str, width: int) -> str:
    """
    依顯示寬度補空白。

    中文字在終端機佔兩格，但 str 只算一個字元，所以 f"{label:<14}" 排出來
    是歪的。用 east_asian_width 判斷全形（W／F）算兩格。
    """
    shown = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(0, width - shown)


@dataclass
class Tally:
    """一張表的匯入結果。跑第二次時 inserted/updated 應該都是 0。"""

    label: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged

    def line(self) -> str:
        changed = (f"新增 {self.inserted:>5,}   更新 {self.updated:>4,}"
                   if self.inserted or self.updated else "無變動")
        return f"  {_pad(self.label, 16)}{self.total:>7,} 筆    {changed}"


def _apply(row, values: dict) -> bool:
    """把 values 寫進 row，回傳有沒有真的改到東西。"""
    changed = False
    for key, value in values.items():
        if getattr(row, key) != value:
            setattr(row, key, value)
            changed = True
    return changed


# --------------------------------------------------------------------------
# 代碼表
# --------------------------------------------------------------------------

def read_code_csv(stem: str) -> list[tuple[str, str]]:
    """讀 data/codes/<stem>.csv。encoding 是 utf-8-sig —— 輸出時帶了 BOM 給 Excel。"""
    path = CODES_DIR / f"{stem}.csv"
    if not path.exists():
        raise SeedImportError(
            f"找不到代碼表：{path}\n"
            f"這幾個 CSV 應該在版控裡。若確實遺失，把官方 .ods 放到專案根目錄後執行：\n"
            f"    python scripts/extract_codes.py"
        )

    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = [(r["code"].strip(), r["name"].strip()) for r in csv.DictReader(f)]

    if not rows:
        raise SeedImportError(f"{path} 沒有任何資料列")

    codes = [c for c, _ in rows]
    if len(codes) != len(set(codes)):
        raise SeedImportError(
            f"{path} 有重複代碼。代碼是主鍵，重複表示 CSV 被手改過 —— "
            f"請改用 scripts/extract_codes.py 重新產生。"
        )
    return rows


def import_codes(session: Session) -> list[Tally]:
    tallies = []
    for stem, model, label in CODE_TABLES:
        rows = read_code_csv(stem)
        existing = {r.code: r for r in session.scalars(select(model)).all()}
        tally = Tally(f"{label}代碼")

        for code, name in rows:
            row = existing.get(code)
            if row is None:
                session.add(model(code=code, name=name))
                tally.inserted += 1
            elif _apply(row, {"name": name}):
                tally.updated += 1
            else:
                tally.unchanged += 1

        tallies.append(tally)
    return tallies


# --------------------------------------------------------------------------
# GWP／公告資訊／係數
# --------------------------------------------------------------------------

def import_gwp(session: Session, data: SeedData) -> Tally:
    """自然鍵 (standard, gas_name)，與 GwpValue 的 UniqueConstraint 相同。"""
    existing = {(r.standard, r.gas_name): r for r in session.scalars(select(GwpValue)).all()}
    tally = Tally("GWP")

    for row in data.gwp:
        values = {
            "formula": row.formula,
            "gwp100": row.gwp100,
            "note": row.note,
        }
        found = existing.get((row.standard, row.gas_name))
        if found is None:
            session.add(GwpValue(standard=row.standard, gas_name=row.gas_name, **values))
            tally.inserted += 1
        elif _apply(found, values):
            tally.updated += 1
        else:
            tally.unchanged += 1
    return tally


def import_factor_set(session: Session, data: SeedData) -> tuple[FactorSet, Tally]:
    """
    一份公告 = 一筆 FactorSet。自然鍵 (name, version)，version 用公告文號。

    公告文號本來就是那一版的唯一識別，拿它當 version，改版時自然會變成
    新的一筆而不是蓋掉舊的 —— 這正是 models.py 寫的「改版＝新增一筆，
    永不 UPDATE 舊的」。
    """
    src = data.factor_source
    name, version = src.announcement, src.doc_no

    values = {
        "doc_no": src.doc_no,
        "publish_date": src.publish_date,
        "gwp_standard": data.gwp_standard,
        "note": src.description,
    }

    found = session.scalars(
        select(FactorSet).where(FactorSet.name == name, FactorSet.version == version)
    ).one_or_none()

    tally = Tally("公告版本")
    if found is None:
        found = FactorSet(name=name, version=version, **values)
        session.add(found)
        session.flush()          # 取得 id，下面 PublishedFactor 要用
        tally.inserted += 1
    elif _apply(found, values):
        tally.updated += 1
    else:
        tally.unchanged += 1

    return found, tally


def import_published_factors(session: Session, data: SeedData,
                             factor_set: FactorSet) -> Tally:
    """
    附表一的 12 個燃料，單位 kg/TJ。

    自然鍵 (factor_set_id, factor_key)：同一份公告裡係數編號不重複，
    不同公告則各有一組。
    """
    existing = {
        r.factor_key: r for r in session.scalars(
            select(PublishedFactor).where(
                PublishedFactor.factor_set_id == factor_set.id)
        ).all()
    }
    tally = Tally("燃料係數")

    for fuel in data.fuels:
        values = {
            "material_code": fuel.material_code,
            "display_name": fuel.display_name,
            "usage": FuelUsage(fuel.usage),
            "vehicle_tech": fuel.vehicle_tech,
            "co2_kg_per_tj": fuel.co2_kg_per_tj,
            "ch4_kg_per_tj": fuel.ch4_kg_per_tj,
            "n2o_kg_per_tj": fuel.n2o_kg_per_tj,
            "ch4_gwp_gas": fuel.ch4_gwp_gas,
            "source_ref": fuel.source_ref,
            "keywords": fuel.keywords,
            "note": fuel.note,
        }
        found = existing.get(fuel.factor_code)
        if found is None:
            session.add(PublishedFactor(
                factor_set_id=factor_set.id, factor_key=fuel.factor_code, **values))
            tally.inserted += 1
        elif _apply(found, values):
            tally.updated += 1
        else:
            tally.unchanged += 1
    return tally


# --------------------------------------------------------------------------
# 熱值
# --------------------------------------------------------------------------

def collapse_heating_values(fuels: list[FuelRow]) -> list[FuelRow]:
    """
    12 個燃料列 → 6 筆熱值，依原燃物料代碼去重。

    多個燃料列共用同一個代碼：汽油的固定燃燒與三種車輛技術別都是
    170001，熱值一樣是 7,520 kcal/公升。熱值是「這個燃料的物理性質」，
    跟固定／移動、跟車輛技術別都無關，所以資料庫裡只該有一筆。

    去重前要檢查共用同一代碼的列，熱值與單位是否真的一致。不一致代表
    試算表自己打架 —— 此時明確報錯，不可任選一筆。選錯的那一筆會安靜地
    讓某些燃料的係數整組偏掉。
    """
    by_code: dict[str, FuelRow] = {}

    for fuel in fuels:
        first = by_code.get(fuel.material_code)
        if first is None:
            by_code[fuel.material_code] = fuel
            continue

        if (first.heating_value_kcal, first.unit) != (fuel.heating_value_kcal, fuel.unit):
            raise SeedImportError(
                f"原燃物料代碼 {fuel.material_code} 在試算表裡有兩組不一致的熱值：\n"
                f"    {first.factor_code}：{first.heating_value_kcal:g} kcal/{first.unit}\n"
                f"    {fuel.factor_code}：{fuel.heating_value_kcal:g} kcal/{fuel.unit}\n"
                f"同一種燃料的熱值不該因用途而異，請先修正試算表。"
            )

    return list(by_code.values())


def import_heating_values(session: Session, data: SeedData) -> Tally:
    """
    自然鍵 (material_code, org_id)。org_id 為 None＝系統預設值。

    事業自行提供的熱值（org_id 有值）不在種子資料範圍內，也絕不能被
    這支腳本蓋掉 —— 那是使用者的資料。where 條件把它們排除在外。
    """
    existing = {
        r.material_code: r for r in session.scalars(
            select(HeatingValue).where(HeatingValue.org_id.is_(None))
        ).all()
    }
    tally = Tally("燃料熱值")

    for fuel in collapse_heating_values(data.fuels):
        values = {
            "factor_key": fuel.material_code,
            "display_name": fuel.display_name,
            "unit": fuel.unit,
            "kcal_per_unit": fuel.heating_value_kcal,
            "source": fuel.heating_value_source,
            "version": fuel.heating_value_version,
            "is_user_override": False,
        }
        found = existing.get(fuel.material_code)
        if found is None:
            session.add(HeatingValue(material_code=fuel.material_code, **values))
            tally.inserted += 1
        elif _apply(found, values):
            tally.updated += 1
        else:
            tally.unchanged += 1
    return tally


# --------------------------------------------------------------------------
# 電力
# --------------------------------------------------------------------------

def import_electricity(session: Session, data: SeedData) -> Tally:
    """自然鍵 year_roc，與 ElectricityFactor 的 unique=True 相同。"""
    existing = {
        r.year_roc: r for r in session.scalars(select(ElectricityFactor)).all()
    }
    tally = Tally("電力係數")

    for row in data.electricity:
        values = {
            "factor_key": row.factor_code,
            "kgco2e_per_kwh": row.kgco2e_per_kwh,
            "source": row.source,
            "note": row.note,
        }
        found = existing.get(row.year_roc)
        if found is None:
            session.add(ElectricityFactor(year_roc=row.year_roc, **values))
            tally.inserted += 1
        elif _apply(found, values):
            tally.updated += 1
        else:
            tally.unchanged += 1
    return tally


# --------------------------------------------------------------------------

def import_all(session: Session, data: SeedData) -> list[Tally]:
    """
    順序即依賴：代碼表 → GWP → 公告版本 → 係數／熱值 → 電力。

    整批在同一個 transaction 裡，任何一步拋錯就全部回滾。匯入到一半的
    資料庫比空的資料庫危險 —— 它看起來是能用的。
    """
    tallies = import_codes(session)
    session.flush()              # 代碼表先落地，後面的外鍵才指得到

    tallies.append(import_gwp(session, data))
    factor_set, fs_tally = import_factor_set(session, data)
    tallies.append(fs_tally)
    tallies.append(import_published_factors(session, data, factor_set))
    tallies.append(import_heating_values(session, data))
    tallies.append(import_electricity(session, data))
    return tallies


def main(argv: list[str]) -> int:
    workbook = Path(argv[1]) if len(argv) > 1 else DEFAULT_WORKBOOK

    try:
        data = read_seed(workbook)
    except (FileNotFoundError, ValueError) as exc:
        print(f"讀取試算表失敗：{exc}", file=sys.stderr)
        return 1

    init_db()

    session = SessionLocal()
    try:
        tallies = import_all(session, data)
        session.commit()
    except Exception as exc:                     # noqa: BLE001 —— 一律回滾後往上報
        session.rollback()
        print(f"匯入失敗，已全部回滾：{exc}", file=sys.stderr)
        return 1
    finally:
        session.close()

    src = data.factor_source
    print(f"公告：{src.announcement}　{src.doc_no}（{src.publish_date_roc}）")
    print(f"GWP ：{data.gwp_standard}")
    print()
    for tally in tallies:
        print(tally.line())
    print()

    if data.electricity_pending_years:
        years = "、".join(f"{y}" for y in data.electricity_pending_years)
        print(f"⚠ {years} 年度電力係數尚未公告，未匯入。")
        print(f"  盤查該年度會查無係數 —— 這是政府還沒公告，不是程式出錯。")
        print()

    print("完成。再跑一次應該全部顯示「無變動」。")
    print("可用 DB Browser for SQLite 打開 carbon.db 檢視。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
