"""建表檢查：確認 models.py 的 13 張表都能真的建立。"""
from app.db import engine, init_db
from sqlalchemy import inspect

init_db()
insp = inspect(engine)
tables = sorted(insp.get_table_names())
print(f"成功建立 {len(tables)} 張資料表：\n")
for t in tables:
    print(f"  {t:<24} {len(insp.get_columns(t))} 欄")
print("\n資料庫檔案：carbon.db（可用 DB Browser for SQLite 打開來看）")
