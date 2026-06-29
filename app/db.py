import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as SessionType
from sqlalchemy.orm import sessionmaker

from app.models import Base


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/fastapi_leads.sqlite3")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

if DATABASE_URL.startswith("sqlite:///"):
    sqlite_path = DATABASE_URL.removeprefix("sqlite:///")
    if sqlite_path and sqlite_path != ":memory:":
        Path(sqlite_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
    future=True,
)
Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    last_error: Exception | None = None
    for _ in range(20):
        try:
            Base.metadata.create_all(engine)
            return
        except SQLAlchemyError as exc:
            last_error = exc
            time.sleep(1)
    if last_error:
        raise last_error


@contextmanager
def session_scope() -> Iterator[SessionType]:
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
