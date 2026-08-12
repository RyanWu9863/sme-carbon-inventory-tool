"""
資料模型層的行為測試 —— 目前只有時間戳記。

時間戳記是稽核用的（這份報告什麼時候算的）。算錯了畫面上看不出來，
所以要測。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Organization, utcnow


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def test_utcnow_is_timezone_aware():
    """
    dt.datetime.utcnow() 回傳的是「UTC 的時刻卻沒有時區標記」的 naive
    datetime，跟本地時間一比就靜靜差 8 小時。改用帶時區的版本。
    """
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == dt.timedelta(0)


def test_timestamp_survives_the_database_round_trip(session):
    """
    **這一個是 SQLite 特有的坑。**

    SQLAlchemy 的 `DateTime(timezone=True)` 在 SQLite 上是空頭支票：寫進去
    帶時區，讀出來 tzinfo 是 None。值沒錯，但型別跟 PostgreSQL 不一樣，
    db.py 說的「換資料庫只要改 CARBON_DB_URL」就不成立。UtcDateTime 這一層
    就是為了補這個洞，所以要有測試守著，否則哪天有人把它拿掉不會有人發現。
    """
    session.add(Organization(name="測試事業", reporting_year_roc=113))
    session.commit()

    org = session.scalars(select(Organization)).one()

    assert org.created_at.tzinfo is not None, "從 SQLite 讀回來時區掉了"
    assert org.created_at.utcoffset() == dt.timedelta(0)
    assert abs((utcnow() - org.created_at).total_seconds()) < 60


def test_naive_datetime_is_refused(session):
    """
    naive datetime 擋在寫入端。放它進來，資料庫裡就會混著 UTC 與 UTC+8
    兩種時刻，而且事後分不出來哪筆是哪種。

    注意拋出來的是 SQLAlchemy 的 StatementError，不是原本的 ValueError ——
    型別轉換發生在送進 driver 的路上，SQLAlchemy 會把例外包起來並附上 SQL。
    原始訊息仍在 __cause__ 裡。
    """
    session.add(Organization(
        name="測試事業", reporting_year_roc=113,
        created_at=dt.datetime(2026, 1, 1, 12, 0, 0),      # 沒有 tzinfo
    ))

    with pytest.raises(StatementError) as exc:
        session.commit()

    assert "naive" in str(exc.value)
    assert isinstance(exc.value.orig, ValueError)


def test_non_utc_timezone_is_normalised(session):
    """帶 UTC+8 寫進去，讀出來要是等值的 UTC，而不是原封不動的 +08:00。"""
    taipei = dt.timezone(dt.timedelta(hours=8))
    session.add(Organization(
        name="測試事業", reporting_year_roc=113,
        created_at=dt.datetime(2026, 1, 1, 20, 0, 0, tzinfo=taipei),
    ))
    session.commit()

    org = session.scalars(select(Organization)).one()
    assert org.created_at.utcoffset() == dt.timedelta(0)
    assert org.created_at == dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
