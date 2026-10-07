"""Process-wide mutation lock shared by imports, classification and backups."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool


ADVISORY_LOCK_KEY = 18180001
_local_lock = threading.RLock()
_held = threading.local()


@contextmanager
def mutation_lock(db: Session) -> Iterator[None]:
    """Hold a session-level lock across commits for a complete mutation.

    A transaction-level advisory lock would be released by the backup service's
    status commits.  Session-level PostgreSQL locking therefore pairs explicit
    ``pg_advisory_lock``/``pg_advisory_unlock`` on a dedicated connection. The
    business Session may commit or roll back without releasing that connection.
    SQLite uses a process lock; multi-process SQLite is not a supported write
    deployment and should use PostgreSQL instead.
    """
    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)
    with _local_lock:
        depths = getattr(_held, "depths", None)
        if depths is None:
            depths = _held.depths = {}
        # Nested services in one thread re-use the outer lock, including when
        # they use a new Session from the same engine.
        url = getattr(engine, "url", None)
        key = (url.get_backend_name(), url.host, url.port, url.database) if url is not None else id(engine)
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        if engine.dialect.name != "postgresql":
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
            return
        # Do not borrow a business-pool connection: concurrent requests may
        # already hold those while waiting for this lock after authentication.
        # NullPool also ensures a failed unlock never leaves a locked session
        # idle in any pool.
        lock_engine = create_engine(url, poolclass=NullPool) if url is not None else engine
        connection = lock_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        acquired = False
        try:
            connection.exec_driver_sql("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            acquired = True
            depths[key] = 1
            yield
        finally:
            depths.pop(key, None)
            if acquired:
                try:
                    unlocked = connection.exec_driver_sql("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,)).scalar_one()
                    if not unlocked:
                        connection.invalidate()
                except Exception:
                    # Never return a potentially locked DB session to the pool.
                    connection.invalidate()
            connection.close()
            if lock_engine is not engine:
                lock_engine.dispose()
