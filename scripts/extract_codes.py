"""
從官方「溫室氣體排放量清冊表單」抽出附表五～七的代碼表，輸出成 CSV。

    python scripts/extract_codes.py

官方 .ods 有 916 KB，且內嵌原始製表者的內部路徑（file:///L:/...），
.gitignore 已擋。版控裡放的是這支腳本抽出來的三個 CSV —— 小、可 diff、
可 code review。CSV 進版控之後，.ods 就不必再跟著專案跑。

為什麼自己解 XML，不裝 odfpy 或 pandas：

    這支腳本一輩子只跑幾次（官方改版時再跑一次），卻會讓每個 clone 這個
    repo 的人都得多裝一個套件。ODS 就是一個 zip 裝著 content.xml，要的
    只是三張兩欄的表，標準函式庫夠用。

抽代碼表看起來是純體力活，實際上有兩個會安靜出錯的地方：儲存格壓縮
（見 _row_values）與官方檔案自己的重複列（見 dedupe）。兩個都不會拋
例外，只會給你一份看起來很正常的錯資料。
"""

from __future__ import annotations

import csv
import datetime as dt
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ODS = ROOT / "溫室氣體排放量清冊表單(範例).ods"
OUT_DIR = ROOT / "data" / "codes"

# 官方表單版本。抽出來的 CSV 要能回答「這是哪一版」。
SOURCE_NAME = "行政院環境部　溫室氣體排放量清冊表單(範例)"
SOURCE_VERSION = "2026/6/1 修正版"

TABLE_NS = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"

# 輸出檔名 → (分頁名稱, 代碼欄標題, 名稱欄標題)
# 檔名刻意與 models.py 的 __tablename__ 相同，匯入時一個檔對一張表。
SHEETS: dict[str, tuple[str, str, str]] = {
    "process_code": ("附表五", "製程代碼", "製程名稱"),
    "equipment_code": ("附表六", "設備代碼", "設備名稱"),
    "material_code": ("附表七", "原燃物料代碼", "原燃物料名稱"),
}

CSV_HEADER = ["official_seq", "code", "name"]


class ExtractError(RuntimeError):
    """官方表單版面與預期不符。與其抽出半套資料，不如在這裡停下來。"""


@dataclass(frozen=True)
class OfficialCode:
    """代碼表的一列。official_seq 保留下來當溯源用，見 write_csv 的說明。"""

    official_seq: int
    code: str
    name: str


@dataclass(frozen=True)
class RemovedDuplicate:
    """
    被移除的重複列，以及它跟哪一筆重複。

    兩個都記下來，是為了讓 README 講得出「6223 與 6179 重複」這種具體的話。
    只記被移除的序號，然後靠算術去推原始列在哪，會推錯 —— 這兩段中間還隔著
    11 筆 GG 代碼，差值不是 33。
    """

    row: OfficialCode
    duplicate_of: OfficialCode


# --------------------------------------------------------------------------
# ODS 解析
# --------------------------------------------------------------------------

def _cell_text(cell: ET.Element) -> str:
    return "".join(cell.itertext()).strip()


def _row_values(row: ET.Element, limit: int) -> list[str]:
    """
    取一列的前 limit 欄，不足補空字串。

    ODS 會把連續相同的儲存格壓成一個，用 number-columns-repeated 記次數。
    一列尾端的空白通常是「一個空儲存格，重複 16384 次」。兩種天真的寫法
    都會出事：

        照實展開全部  → 一列膨脹成 16384 個元素，6255 列直接炸掉記憶體
        直接無視 repeat → 欄位左移，讀到的資料看起來完全正常，只是全錯

    第二種才可怕，因為它不會有任何錯誤訊息。這裡照實展開，但取滿 limit
    欄就停 —— 需要的只有 序號／代碼／名稱 三欄，後面再多都與我們無關。
    """
    out: list[str] = []
    for cell in row.findall(f"{TABLE_NS}table-cell"):
        if len(out) >= limit:
            break
        repeat = int(cell.get(f"{TABLE_NS}number-columns-repeated", "1"))
        out.extend([_cell_text(cell)] * min(repeat, limit - len(out)))
    return out + [""] * (limit - len(out))


def _find_sheet(content: ET.Element, name: str) -> ET.Element:
    """
    依分頁名稱找出 table 元素。

    這份 .ods 裡除了 19 張真正的分頁，還有上百個指向原始製表者本機磁碟
    （file:///L:/...）的外部連結，在 XML 裡同樣是 table 元素。用完整名稱
    精確比對，不要用模糊搜尋。
    """
    for table in content.iter(f"{TABLE_NS}table"):
        if table.get(f"{TABLE_NS}name") == name:
            return table
    raise ExtractError(f"官方表單裡找不到分頁「{name}」，請確認檔案版本。")


def parse_sheet(table: ET.Element, sheet_name: str,
                code_header: str, name_header: str) -> list[OfficialCode]:
    """解析一張代碼表分頁。版面不符或讀漏列都在這裡拋 ExtractError。"""
    rows = table.findall(f"{TABLE_NS}table-row")
    if not rows:
        raise ExtractError(f"分頁「{sheet_name}」沒有任何列")

    expected_header = ["序號", code_header, name_header]
    header = _row_values(rows[0], 3)
    if header != expected_header:
        raise ExtractError(
            f"分頁「{sheet_name}」標題列應為 "
            f"{'｜'.join(expected_header)}，實際為 {'｜'.join(header)}。"
            f"官方表單版面可能改過，請確認版本。"
        )

    out: list[OfficialCode] = []
    for row in rows[1:]:
        seq, code, name = _row_values(row, 3)
        if not seq.isdigit():
            continue                      # 表格下方的空白列
        if not code or not name:
            raise ExtractError(
                f"分頁「{sheet_name}」序號 {seq} 的代碼或名稱是空的："
                f"代碼={code!r} 名稱={name!r}"
            )
        out.append(OfficialCode(int(seq), code, name))

    _check_sequence_is_complete(out, sheet_name)
    return out


def _check_sequence_is_complete(rows: list[OfficialCode], sheet_name: str) -> None:
    """
    序號必須是 1..N 連續無跳號。

    這是整支腳本最重要的一道檢查。解析錯誤不會拋例外，只會少幾筆 ——
    6255 讀成 6250，你不會發現，而且錯誤會一路帶進資料庫。官方表的序號
    本來就連續，拿它當校驗碼，就不必事先知道正確筆數也能證明沒讀漏。

    這一道同時擋掉三種失敗：漏讀、重複讀、順序錯亂。
    """
    if not rows:
        raise ExtractError(f"分頁「{sheet_name}」讀不到任何資料列")

    for position, row in enumerate(rows, start=1):
        if row.official_seq != position:
            raise ExtractError(
                f"分頁「{sheet_name}」序號不連續：讀到的第 {position} 筆，"
                f"序號卻是 {row.official_seq}（{row.code} {row.name}）。"
                f"表示解析漏讀或重複讀了列，抽出來的代碼表不完整。"
            )


# --------------------------------------------------------------------------
# 去重
# --------------------------------------------------------------------------

def dedupe(rows: list[OfficialCode],
           sheet_name: str) -> tuple[list[OfficialCode], list[RemovedDuplicate]]:
    """
    移除官方檔案自己的重複列，回傳 (保留的, 移除的)。

    附表七把序號 6179~6211 那一段（廢液與污染土壤，R-25xx／S-01xx／S-02xx）
    原封不動複製了第二次成為 6223~6255，共 33 筆。MaterialCode.code 是主鍵，
    不處理會直接擋住匯入。

    兩種重複分開處理：

        同代碼 + 同名稱   → 保留序號小的，其餘視為重複貼上
        同代碼 + 不同名稱 → 拋錯

    後者現在一筆都沒有。但真的出現時，代表官方改了名稱或代碼被重用，
    該由人判斷保留哪一個 —— 程式替你挑一個，等於把一個需要決策的問題
    變成一個看不見的問題。

    保留順序＝官方原始順序，這樣跟未來新版官方表 diff 才有意義。
    """
    kept: dict[str, OfficialCode] = {}
    removed: list[RemovedDuplicate] = []

    for row in rows:
        first = kept.get(row.code)
        if first is None:
            kept[row.code] = row
        elif first.name == row.name:
            removed.append(RemovedDuplicate(row=row, duplicate_of=first))
        else:
            raise ExtractError(
                f"分頁「{sheet_name}」代碼 {row.code} 出現兩次，名稱卻不同：\n"
                f"    序號 {first.official_seq}：{first.name}\n"
                f"    序號 {row.official_seq}：{row.name}\n"
                f"這不是單純的重複貼上，需要人工判斷保留哪一個。"
            )

    return list(kept.values()), removed


# --------------------------------------------------------------------------
# 輸出
# --------------------------------------------------------------------------

def write_csv(path: Path, rows: list[OfficialCode]) -> None:
    """
    寫出 CSV。

    encoding 用 utf-8-sig（帶 BOM）—— 這幾個檔會被用 Excel 開來目視檢查，
    Windows 的 Excel 看到沒有 BOM 的 UTF-8 CSV 會顯示成亂碼。

    lineterminator 固定 "\\n"，避免在 Windows 產生 CRLF 讓 diff 變髒。

    official_seq 欄留著是為了溯源 —— 拿著序號可以直接翻回官方表單對照。
    附表七這一版的重複列剛好都在尾端（6223~6255），所以留下來的序號仍是
    連續的 1~6222；若未來版本的重複出現在中間，序號就會出現缺口，那個
    缺口本身就是「這裡刪過東西」的線索。詳細刪除紀錄在 README.md。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        for row in rows:
            writer.writerow([row.official_seq, row.code, row.name])


def write_readme(path: Path, report: dict[str, dict]) -> None:
    """
    產生 data/codes/README.md。

    來源註記不寫進 CSV 的註解行，因為 CSV 沒有標準的註解語法，csv 模組
    會把 # 開頭的行當成資料列讀進來。放在 sidecar 讓 CSV 保持乾淨可直接
    解析，而且 GitHub 進到這個資料夾會自動渲染 README，找得到。
    """
    lines = [
        "# 官方代碼表（附表五～七）",
        "",
        f"- **來源**：{SOURCE_NAME}",
        f"- **版本**：{SOURCE_VERSION}",
        f"- **抽取日期**：{dt.date.today().isoformat()}",
        "- **產生方式**：`python scripts/extract_codes.py`（不要手改這幾個 CSV）",
        "",
        "官方 .ods 原檔 916 KB 且內嵌原始製表者的內部路徑，未進版控",
        "（`.gitignore` 已擋）。這裡的 CSV 就是它的全部有用內容。",
        "",
        "## 筆數",
        "",
        "| 檔案 | 官方分頁 | 原始 | 去重 | 保留 |",
        "|---|---|---:|---:|---:|",
    ]

    total = 0
    for stem, info in report.items():
        sheet = SHEETS[stem][0]
        total += info["kept"]
        lines.append(
            f"| `{stem}.csv` | {sheet} | {info['raw']:,} | "
            f"{info['removed_count']:,} | **{info['kept']:,}** |"
        )
    lines += [f"| | | | | **合計 {total:,}** |", ""]

    lines += [
        "## 欄位",
        "",
        "| 欄位 | 說明 |",
        "|---|---|",
        "| `official_seq` | 官方表單上的序號，拿著它可以翻回原表對照 |",
        "| `code` | 代碼，對應 `models.py` 的主鍵 |",
        "| `name` | 名稱 |",
        "",
    ]

    removed_any = {s: i for s, i in report.items() if i["removed"]}
    lines += ["## 去重紀錄", ""]

    if not removed_any:
        lines += ["三張表都沒有重複代碼。", ""]
    else:
        lines += [
            "官方表單本身有重複列 —— 同一段內容被複製貼上兩次，代碼與名稱",
            "完全相同。代碼是資料庫主鍵，重複會直接擋住匯入，因此在抽取階段",
            "移除，並在這裡留下紀錄。",
            "",
            "**只有「同代碼且同名稱」才移除。** 若同一代碼對到不同名稱，",
            "`extract_codes.py` 會拋錯而不是自行挑一個。",
            "",
        ]
        for stem, info in removed_any.items():
            sheet = SHEETS[stem][0]
            dups = info["removed"]
            gone = [d.row.official_seq for d in dups]
            orig = [d.duplicate_of.official_seq for d in dups]
            lines += [
                f"### {sheet}（`{stem}.csv`）— 移除 {len(dups)} 筆",
                "",
                f"官方序號 {min(gone)}～{max(gone)} 這一段，與序號 "
                f"{min(orig)}～{max(orig)} 逐筆同代碼同名稱。",
                "",
                "| 移除的序號 | 保留的序號 | 代碼 | 名稱 |",
                "|---:|---:|---|---|",
            ]
            lines += [
                f"| {d.row.official_seq} | {d.duplicate_of.official_seq} "
                f"| `{d.row.code}` | {d.row.name} |"
                for d in dups
            ]
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------

def extract(ods_path: Path, out_dir: Path) -> dict[str, dict]:
    """抽出三張代碼表並寫檔，回傳各表的筆數與去重明細。"""
    if not ods_path.exists():
        raise FileNotFoundError(
            f"找不到官方表單：{ods_path}\n"
            f"這個檔案不在版控裡（916 KB 且內嵌原始製表者的內部路徑），"
            f"需要自行取得後放到專案根目錄。\n"
            f"若只是要用代碼表，data/codes/*.csv 已經在版控裡，不需要這個檔。"
        )

    with zipfile.ZipFile(ods_path) as z:
        content = ET.fromstring(z.read("content.xml"))

    report: dict[str, dict] = {}
    for stem, (sheet_name, code_header, name_header) in SHEETS.items():
        table = _find_sheet(content, sheet_name)
        rows = parse_sheet(table, sheet_name, code_header, name_header)
        kept, removed = dedupe(rows, sheet_name)

        write_csv(out_dir / f"{stem}.csv", kept)
        report[stem] = {
            "sheet": sheet_name,
            "raw": len(rows),
            "kept": len(kept),
            "removed_count": len(removed),
            "removed": removed,
        }

    write_readme(out_dir / "README.md", report)
    return report


def main(argv: list[str]) -> int:
    ods_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_ODS

    try:
        report = extract(ods_path, OUT_DIR)
    except (ExtractError, FileNotFoundError) as exc:
        print(f"抽取失敗：{exc}", file=sys.stderr)
        return 1

    print(f"來源：{SOURCE_NAME}　{SOURCE_VERSION}")
    print(f"輸出：{OUT_DIR}")
    print()
    for stem, info in report.items():
        note = f"（去重 {info['removed_count']}）" if info["removed_count"] else ""
        print(
            f"  {info['sheet']}  {stem}.csv"
            f"　原始 {info['raw']:>5,} → 保留 {info['kept']:>5,} {note}"
        )
    print()
    print(f"  合計 {sum(i['kept'] for i in report.values()):,} 筆")
    print()
    print("代碼表已進版控，官方 .ods 原檔不需要再帶著跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
