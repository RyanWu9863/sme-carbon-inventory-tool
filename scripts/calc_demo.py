"""
算示範案例的年度排放量，印出表八。

    python scripts/calc_demo.py

前置：
    python scripts/import_seed.py     官方係數與代碼表
    python scripts/load_demo.py       示範小吃店

驗收基準是 **7.531736 tCO2e** —— 與 v5 試算表表八相同，但這一次是從資料庫
算出來的。試算表那邊是 Excel 公式，這邊是 Python 走 ORM 查係數再計算，
兩條路徑完全獨立。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select                                    # noqa: E402

from app.db import SessionLocal                                  # noqa: E402
from app.models import Organization                              # noqa: E402
from app.service import (                                        # noqa: E402
    ServiceError, calculate_year, format_summary,
)

TARGET_TCO2E = 7.531736


def main() -> int:
    session = SessionLocal()
    try:
        orgs = session.scalars(select(Organization)).all()
        if not orgs:
            print(
                "資料庫裡沒有任何事業。先執行：\n"
                "    python scripts/import_seed.py\n"
                "    python scripts/load_demo.py",
                file=sys.stderr,
            )
            return 1

        for org in orgs:
            try:
                summary = calculate_year(session, org)
            except ServiceError as exc:
                print(f"計算失敗：{exc}", file=sys.stderr)
                return 1

            session.commit()
            print(format_summary(summary))
            print()

            diff = abs(summary.total_tco2e - TARGET_TCO2E)
            mark = "OK" if diff < 5e-7 else "!!"
            print(f"  [{mark}] 對照 v5 試算表表八 {TARGET_TCO2E} tCO2e，"
                  f"差 {diff:.2e}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
