# 碳盤查系統 — 安裝與測試

對齊環境部「溫室氣體排放量清冊表單」的中小企業碳盤查工具。這一份講怎麼裝、怎麼跑；
**做到哪一步、為什麼這樣設計、下一步做什麼，看 [PROGRESS.md](PROGRESS.md)。**

## 檔案該放哪

下載的檔案要照這個結構擺，位置錯了 import 會失敗：

```
carbon/                      ← 專案根目錄，名字隨你取
├── requirements.txt
├── check_schema.py
├── 碳盤查試算表_v5.xlsx      ← 所有數字的基準，測試與種子資料都讀它
├── app/
│   ├── __init__.py          ← 空檔案，但一定要有
│   ├── db.py
│   ├── models.py
│   ├── calculator.py
│   └── seed.py
├── scripts/
│   ├── __init__.py          ← 空檔案，但一定要有
│   ├── extract_codes.py     ← 一次性工具，代碼表已抽好，平常不必跑
│   └── import_seed.py       ← 把種子資料寫進 carbon.db
├── data/codes/              ← 官方代碼表，7,603 筆，已進版控
│   ├── process_code.csv
│   ├── equipment_code.csv
│   ├── material_code.csv
│   └── README.md            ← 來源、版本、去重紀錄
└── tests/
    ├── __init__.py          ← 空檔案，但一定要有
    ├── test_calculator.py
    ├── test_seed.py
    ├── test_codes.py
    ├── test_import_seed.py
    └── test_models.py
```

`__init__.py` 是三個**完全空白**的檔案，Python 靠它辨認資料夾是套件。漏了會出現
`ModuleNotFoundError: No module named 'app'`。

建立空檔案：

```bash
# macOS / Linux
touch app/__init__.py tests/__init__.py scripts/__init__.py

# Windows PowerShell
New-Item app/__init__.py -ItemType File
New-Item tests/__init__.py -ItemType File
New-Item scripts/__init__.py -ItemType File
```

## 環境需求

Python 3.10 以上。確認版本：

```bash
python --version
```

Windows 若 `python` 沒反應，改用 `py --version`，後面的指令也把 `python` 換成 `py`。

## 安裝

在專案根目錄開終端機：

```bash
# 1. 建虛擬環境（避免污染系統 Python）
python -m venv .venv

# 2. 啟用
source .venv/bin/activate      # macOS / Linux
.venv\Scripts\activate         # Windows PowerShell

# 3. 安裝套件
pip install -r requirements.txt
```

啟用成功的話，命令列前面會出現 `(.venv)`。

## 執行測試

**一定要在專案根目錄執行**，不要 `cd` 進 tests 資料夾。

```bash
python -m pytest tests/ -v
```

預期輸出：

```
======================== test session starts ========================
collected 66 items

tests/test_calculator.py::test_derived_factor_matches_spreadsheet[TW-M-GASOLINE-OXI-2.270679] PASSED
tests/test_calculator.py::test_ch4_uses_28_not_30 PASSED
...
tests/test_seed.py::test_every_fuel_matches_the_spreadsheet PASSED
tests/test_codes.py::test_v5_codes_agree_with_the_official_table[material_codes-material_code] PASSED
tests/test_import_seed.py::test_factors_survive_the_round_trip PASSED
======================== 66 passed in 7.02s =========================
```

**看到 `66 passed` 就對了。**

只想看結果不看細節，把 `-v` 換成 `-q`。

## 這 66 個測試在測什麼

### `test_calculator.py` — 計算引擎（17 個）

| 測試 | 驗證的事 |
|---|---|
| `test_derived_factor_matches_spreadsheet` ×5 | 五種燃料推導出的每單位係數，與試算表逐位相同 |
| `test_ch4_uses_28_not_30` | 燃料燃燒採甲烷 GWP=28（依平台公告），非石化甲烷 30 |
| `test_missing_heating_value_fails_loudly` | 缺熱值時明確報錯，不會偷偷用預設值 |
| `test_unknown_unit_raises` | 未知單位報錯，不會假設 1:1 |
| `test_kiloliter_converts` | 公秉 ↔ 公升 換算正確（官方表四以公秉計） |
| `test_cross_year_allocation` | 跨年帳單分攤 45/62 天，且自動標記為推估 |
| `test_period_entirely_outside_year` | 完全在盤查年度外的期間分攤為 0 |
| **`test_totals_match_spreadsheet_table8`** | **總量 7.531736 tCO2e，與試算表表八一致** |
| `test_electricity_factor_not_double_counted` | 電力係數不會被重複乘 GWP |
| `test_completeness_*` ×3 | 完整性檢查抓得到「無資料」與「缺月份」 |

### `test_seed.py` — 種子資料讀取（11 個）

| 測試 | 驗證的事 |
|---|---|
| **`test_every_fuel_matches_the_spreadsheet`** | **全部 12 個燃料，程式重算與試算表 U 欄一致（最大差 4.4e-16）** |
| `test_all_twelve_fuels_are_read` | 少讀一列不會有錯誤訊息，只會少一個燃料可選 |
| `test_conversion_constant_agrees_with_the_code` | 4.1868E-9 在試算表與程式各寫一份，必須相同 |
| `test_fuel_combustion_uses_methane_28` | 種子資料端也守住 28 而非 30 |
| `test_gwp_table_has_the_four_gases` | GWP 表四種氣體齊全 |
| `test_electricity_factors_are_per_year` | 113 年 0.474、112 年 0.494 |
| `test_unpublished_electricity_year_is_reported_not_silently_dropped` | 114 年尚未公告，跳過但不無聲消失 |
| `test_code_blocks_are_read_separately` | 三個代碼區塊並排在同一張表，不可讀混 |
| `test_material_codes_cover_every_fuel` | 燃料的原燃物料代碼都在代碼表裡 |
| `test_missing_workbook_says_what_to_do` | 找不到試算表時說明該怎麼辦 |
| `test_moved_column_is_caught` | 欄位被搬動時擋在門口 |

### `test_codes.py` — 官方代碼表（16 個）

| 測試 | 驗證的事 |
|---|---|
| `test_identical_repeat_is_dropped` | 官方表單自己的重複列（同代碼同名稱）會被移除 |
| `test_same_code_different_name_refuses_to_choose` | 同代碼卻不同名稱時拋錯，不自行挑一個 |
| `test_official_order_is_preserved` | 保留官方原始順序，才能跟未來新版 diff |
| `test_row_count_matches_the_official_form` ×3 | 1,023 / 358 / 6,222 筆 |
| `test_header_and_fields_are_intact` ×3 | 欄位齊全，代碼與名稱都不是空的 |
| `test_codes_are_unique` ×3 | 代碼是主鍵，不可重複 |
| **`test_v5_codes_agree_with_the_official_table`** ×3 | **v5 手挑的 23 筆代碼，與官方全量逐字相同** |
| `test_every_fuel_material_code_is_in_the_official_table` | 12 個燃料的原燃物料代碼都對得到官方表 |

**這一組不需要官方 .ods**：去重邏輯用自編假資料測，其餘測已進版控的 CSV。

### `test_import_seed.py` — 種子資料匯入（18 個）

| 測試 | 驗證的事 |
|---|---|
| **`test_factors_survive_the_round_trip`** | **從資料庫重算 12 個係數，仍與試算表 U 欄一致** |
| **`test_running_twice_changes_nothing`** | **跑兩次結果相同，且第二次完全沒有新增／更新** |
| `test_row_counts` ×8 | 八張表的筆數（1,023／358／6,222／4／1／12／6／2） |
| `test_no_orphan_foreign_keys` | 係數指到的原燃物料代碼都真的存在 |
| `test_shared_material_code_collapses_to_one_heating_value` | 12 個燃料列 → 6 筆熱值 |
| `test_conflicting_heating_values_are_refused` | 同代碼卻兩組熱值時報錯，不任選一筆 |
| `test_factor_set_records_the_announcement` | 公告文號與日期有記進資料庫 |
| `test_fuel_combustion_still_uses_methane_28` | 資料庫端也守住 28 |
| `test_unpublished_electricity_year_is_not_in_the_database` | 114 年度不會憑空出現 |
| `test_user_override_heating_values_are_not_touched` | 匯入不蓋掉事業自填的熱值 |
| `test_missing_code_csv_says_how_to_regenerate` | 少了 CSV 時說明怎麼重新產生 |

**這一組建的是記憶體資料庫**（`sqlite:///:memory:`），不會碰到你手上的 `carbon.db`。

### `test_models.py` — 資料模型（4 個）

| 測試 | 驗證的事 |
|---|---|
| `test_utcnow_is_timezone_aware` | 時間戳記帶時區，不是 naive UTC |
| **`test_timestamp_survives_the_database_round_trip`** | **從 SQLite 讀回來時區還在** |
| `test_naive_datetime_is_refused` | naive datetime 擋在寫入端 |
| `test_non_utc_timezone_is_normalised` | UTC+8 寫進去，讀出來是等值的 UTC |

第二個守的是 SQLite 特有的坑：`DateTime(timezone=True)` 在 SQLite 上是空頭支票，
寫進去帶時區、讀出來卻是 naive。`UtcDateTime` 這一層就是為了補它。

兩個最重要的是 `test_totals_match_spreadsheet_table8` 與
`test_every_fuel_matches_the_spreadsheet`：**程式與試算表算出同一個數字**。
試算表那邊是 Excel 公式，程式這邊是 Python，兩條路徑完全獨立。

## 建立資料表

```bash
python check_schema.py
```

預期輸出 `成功建立 13 張資料表`，並在目錄下產生 `carbon.db`。

## 匯入種子資料

```bash
python scripts/import_seed.py
```

把官方代碼表（`data/codes/*.csv`）與係數／熱值／電力／GWP（v5 試算表）寫進
`carbon.db`。不必先跑 `check_schema.py`，這支會自己建表。

預期輸出：

```
公告：溫室氣體排放係數　環部授氣字第1139101231號（113年2月5日）
GWP ：AR5

  製程代碼          1,023 筆    新增 1,023   更新    0
  設備代碼            358 筆    新增   358   更新    0
  原(燃)物料代碼    6,222 筆    新增 6,222   更新    0
  GWP                   4 筆    新增     4   更新    0
  公告版本              1 筆    新增     1   更新    0
  燃料係數             12 筆    新增    12   更新    0
  燃料熱值              6 筆    新增     6   更新    0
  電力係數              2 筆    新增     2   更新    0
```

**再跑一次應該全部顯示「無變動」** —— 這支腳本依自然鍵 upsert，可重複執行。
會這樣設計是因為刪掉重建會讓已算好的 `EmissionResult` 外鍵指向消失的列。

想用視覺化工具檢視 `carbon.db`，可以裝
[DB Browser for SQLite](https://sqlitebrowser.org/)（免費）。

重建資料庫：刪掉 `carbon.db` 再跑一次 `import_seed.py` 即可。

## 代碼表是怎麼來的

`data/codes/*.csv` 是從官方「溫室氣體排放量清冊表單」的附表五～七抽出來的，
共 7,603 筆。**已經進版控，平常不需要重跑。**

官方 .ods 原檔 916 KB、內嵌原始製表者的內部路徑，`.gitignore` 已擋 —— 也就是說
你 clone 下來不會有那個檔，但代碼表照樣可用，測試也照樣全綠。這是刻意的。

只有官方改版時才需要重跑（把新的 .ods 放到專案根目錄）：

```bash
python scripts/extract_codes.py
```

它會印出各表筆數與去重明細，並更新 `data/codes/README.md`。
版面對不上、序號跳號、同代碼卻不同名稱，都會直接拋錯而不是產出半套資料。

## 測試的相依是分層的

五個測試檔需要的東西不一樣，這是刻意的：

| 測試檔 | 需要 | 為什麼可以這麼輕 |
|---|---|---|
| `test_calculator.py` | 只要 pytest | 計算引擎是純函式 |
| `test_seed.py` | ＋openpyxl | 讀試算表，但不寫任何檔案 |
| `test_codes.py` | ＋openpyxl | 去重是純函式，其餘讀已進版控的 CSV |
| `test_import_seed.py` | ＋SQLAlchemy | 碰資料庫，但建在記憶體裡 |
| `test_models.py` | ＋SQLAlchemy | 同上 |

只跑不需要資料庫的三組（44 個）：

```bash
python -m pytest tests/ -q --ignore=tests/test_import_seed.py --ignore=tests/test_models.py
```

這個分層不是巧合，是設計的結果 —— 會安靜出錯的邏輯（單位換算、係數推導、欄位
對位、代碼去重、熱值去重）全部留在純函式裡，所以不必先有資料庫就測得動。
被問「你怎麼確保計算正確」時，這點可以直接拿來講。

## 常見錯誤

| 錯誤訊息 | 原因與解法 |
|---|---|
| `ModuleNotFoundError: No module named 'app'` | 沒在專案根目錄執行，或缺 `__init__.py` |
| `ModuleNotFoundError: No module named 'pytest'` | 忘了啟用虛擬環境，或沒 `pip install` |
| `ModuleNotFoundError: No module named 'openpyxl'` | `test_seed.py` 要讀試算表，`pip install openpyxl` |
| `ModuleNotFoundError: No module named 'sqlalchemy'` | 只跑測試不需要它；要跑 `check_schema.py` 才需安裝 |
| `FileNotFoundError: 找不到試算表` | `碳盤查試算表_v5.xlsx` 不在專案根目錄 |
| `SeedFormatError: ... 欄位順序可能被更動過` | 試算表的欄位被搬動，或換了不同版本的檔案 |
| `FileNotFoundError: 找不到官方表單` | 只有 `extract_codes.py` 需要 .ods，跑測試不需要 |
| `ExtractError: ... 序號不連續` | 官方表單版面改過，解析漏讀了列，不要放行 |
| `SyntaxError` 出現在型別標註處 | Python 版本低於 3.10，請升級 |
| 中文顯示成亂碼（Windows） | 終端機執行 `chcp 65001` 切換為 UTF-8 |

## 怎麼確認測試「真的有在測」

改壞一個值，測試應該要失敗。試試看：

把 `calculator.py` 裡的

```python
KCAL_TO_TJ = 4.1868e-9
```

改成

```python
KCAL_TO_TJ = 4.1868e-8
```

再跑一次測試，應該會看到 **9 個測試失敗**（`test_calculator.py` 6 個、`test_seed.py`
2 個、`test_import_seed.py` 1 個）。改回來後 66 個全部通過。

那第 9 個是 `test_factors_survive_the_round_trip` —— 它從資料庫的值重算係數，
所以計算引擎改壞了它也跟著紅。這正是它存在的理由。

**測試通過不代表程式對，但改壞了測試卻沒失敗，就一定有問題。**
這個小實驗可以錄進 demo，展示你的驗證機制是有效的。

### ⚠ 做這個實驗會踩到的快取陷阱

`e-9` 改成 `e-8` 檔案大小完全相同，若兩次修改又發生在同一秒內，Python 會誤判
檔案沒變、沿用舊的 `.pyc`，結果是**改回來了但測試還是失敗**。

遇到就清掉快取：

```bash
# macOS / Linux
find app tests -name "__pycache__" -type d -exec rm -rf {} +

# Windows PowerShell
Get-ChildItem -Path app,tests -Filter __pycache__ -Recurse -Directory | Remove-Item -Recurse

# 或直接用 pytest 參數，不寫入快取
python -m pytest tests/ -q -p no:cacheprovider
```

`.gitignore` 已經建好了（`__pycache__`、`.venv`、`carbon.db`、`.pytest_cache` 都擋掉）。

裡面還擋了兩類**不能進 public repo** 的東西，加檔案時留意：

- 個人資料 —— 載具消費明細、發票明細（`載具*.csv`、`消費明細*.csv`、`data/private/`）
- 官方表單原檔（`溫室氣體排放量清冊表單*.ods`）—— 版控裡放的是從它抽出來的代碼表 CSV
