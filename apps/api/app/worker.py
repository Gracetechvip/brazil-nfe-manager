"""Persistent worker. A PostgreSQL session lock elects one live worker."""
import time, logging
from .db import engine
from .migrate import migrate
from .services.jobs import run_pending_once

def main():
    migrate()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as leader:
        if engine.dialect.name=="postgresql":
            while not leader.exec_driver_sql("SELECT pg_try_advisory_lock(18180002)").scalar(): time.sleep(2)
        while True:
            try:
                if not run_pending_once(): time.sleep(1)
            except Exception:
                logging.exception("worker iteration failed");time.sleep(2)

if __name__=="__main__": main()
