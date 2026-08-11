"""資料庫連線。開發用 SQLite，要換 MySQL/PostgreSQL 只需改 CARBON_DB_URL。"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DB_URL = os.getenv("CARBON_DB_URL", "sqlite:///./carbon.db")

engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI 依賴注入用。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """建立所有資料表。第一次執行或刪掉 carbon.db 後執行。"""
    from . import models  # noqa: F401  匯入以註冊 model
    Base.metadata.create_all(bind=engine)
