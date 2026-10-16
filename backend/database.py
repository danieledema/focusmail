import os
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATA_DIR = Path(os.getenv('DATA_DIR', './data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(f'sqlite:///{DATA_DIR / "focusmail.db"}', connect_args={'check_same_thread': False})
@event.listens_for(engine, 'connect')
def sqlite_config(conn, _):
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
Session = sessionmaker(engine, expire_on_commit=False)
class Base(DeclarativeBase):
    pass


def initialize_database():
    Base.metadata.create_all(engine)
    # Additive v1 migration for databases created before sent-folder history support.
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql('PRAGMA table_info(messages)')}
        if 'folder' not in columns:
            conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN folder VARCHAR DEFAULT 'INBOX'")
