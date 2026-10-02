"""Engine and sessions."""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


class Database:
    def __init__(self, url: str):
        # psycopg 3 is the driver; accept the plain postgresql:// form too.
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        self.engine = create_engine(url, pool_pre_ping=True, future=True)
        self._session = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        """Create missing tables. Bootstrap only — see models.py on Alembic."""
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self):
        s: Session = self._session()
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()
