"""Small durable event transport shared by local LangBot plugin processes."""
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path


def connect(path=None):
    path = Path(path or os.environ.get('LANGBOT_BEHAVIOR_DB', '/app/data/plugins/.behavior_bus/events.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        # Bind mounts must not depend on WAL shared memory; inspect in Linux only.
        if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':
            db.execute('PRAGMA journal_mode=DELETE')
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, event_id TEXT UNIQUE, target TEXT, kind TEXT, created REAL, payload TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS leases (target TEXT, resource TEXT, expires REAL, PRIMARY KEY(target,resource))')
        return db
    except Exception:
        db.close()
        raise


def publish(target, kind, payload=None, resource=None, busy=None, path=None):
    """Best effort adapters must never break the originating chat/media task."""
    db = None
    try:
        db = connect(path)
        with db:
            target = str(target).split(':')[-1]
            db.execute('INSERT INTO events(event_id,target,kind,created,payload) VALUES(?,?,?,?,?)',
                       (str(uuid.uuid4()), target, kind, time.time(), json.dumps(payload or {}, ensure_ascii=False)))
            if resource:
                if busy:
                    db.execute('INSERT OR REPLACE INTO leases VALUES(?,?,?)', (target, resource, time.time()+1800))
                else:
                    db.execute('DELETE FROM leases WHERE target=? AND resource=?', (target, resource))
    except Exception:
        logging.getLogger(__name__).exception('Behavior event could not be persisted')
    finally:
        if db is not None:
            db.close()
