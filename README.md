# 碳盤查系統 — 安裝與測試

## 檔案該放哪

下載的檔案要照這個結構擺，位置錯了 import 會失敗：

```
carbon/                      ← 專案根目錄，名字隨你取
├── requirements.txt
├── check_schema.py
├── app/
│   ├── __init__.py          ← 空檔案，但一定要有
│   ├── db.py
│   ├── models.py
│   └── calculator.py
└── tests/
    ├── __init__.py          ← 空檔案，但一定要有
    └── test_calculator.py
```

`__init__.py` 是兩個**完全空白**的檔案，Python 靠它辨認資料夾是套件。漏了會出現
`ModuleNotFoundError: No module named 'app'`。

建立空檔案：

```bash
# macOS / Linux
touch app/__init__.py tests/__init__.py

# Windows PowerShell
New-Item app/__init__.py -ItemType File
New-Item tests/__init__.py -ItemType File
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
collected 17 items

tests/test_calculator.py::test_derived_factor_matches_spreadsheet[TW-M-GASOLINE-OXI-2.270679] PASSED
tests/test_calculator.py::test_ch4_uses_28_not_30 PASSED
...
tests/test_calculator.py::test_totals_match_spreadsheet_table8 PASSED
======================== 17 passed in 0.05s =========================
```

**看到 `17 passed` 就對了。**

只想看結果不看細節，把 `-v` 換成 `-q`。

## 這 17 個測試在測什麼

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

最重要的是 `test_totals_match_spreadsheet_table8`。它是 W2 的完成判準：
**程式與試算表算出同一個數字**。

## 建立資料表

```bash
python check_schema.py
```

預期輸出 `成功建立 13 張資料表`，並在目錄下產生 `carbon.db`。

想用視覺化工具檢視這個檔案，可以裝
[DB Browser for SQLite](https://sqlitebrowser.org/)（免費）。

重建資料庫：刪掉 `carbon.db` 再跑一次即可。

## 只想跑測試的話

`test_calculator.py` **只 import calculator.py**，不碰資料庫。所以：

```bash
pip install pytest        # 這樣就夠，不用裝 SQLAlchemy
python -m pytest tests/ -q
```

這不是巧合，是設計的結果 —— 計算引擎寫成純函式、不依賴資料庫，所以可以獨立測試。
口試若被問「你怎麼確保計算正確」，這點可以直接拿來講。

## 常見錯誤

| 錯誤訊息 | 原因與解法 |
|---|---|
| `ModuleNotFoundError: No module named 'app'` | 沒在專案根目錄執行，或缺 `__init__.py` |
| `ModuleNotFoundError: No module named 'pytest'` | 忘了啟用虛擬環境，或沒 `pip install` |
| `ModuleNotFoundError: No module named 'sqlalchemy'` | 只跑測試不需要它；要跑 `check_schema.py` 才需安裝 |
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

再跑一次測試，應該會看到 6 個測試失敗。改回來後全部通過。

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

順帶一提：`__pycache__`、`.venv`、`carbon.db` 都不要進版控。建一個 `.gitignore`：

```
__pycache__/
*.pyc
.venv/
.pytest_cache/
carbon.db
```
