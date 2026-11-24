"""Shared, atomic appointment snapshots scoped to recipient and bot."""
import json
import os
import sqlite3
import time
from pathlib import Path


def connect(path=None):
    path = Path(path or os.environ.get('LANGBOT_STUDY_BRIDGE_DB', '/app/data/plugins/.study_bridge/state.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS snapshots (target TEXT, bot TEXT, updated REAL, payload TEXT, PRIMARY KEY(target,bot))')
    return db


def publish(target, bot, reminders, enabled, path=None):
    if not target or not bot:
        return
    db = connect(path)
    try:
        with db:
            db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)',
                       (str(target), str(bot), time.time(), json.dumps({'enabled': enabled, 'reminders': reminders}, ensure_ascii=False)))
    finally:
        db.close()


def snapshot(target, bot, path=None):
    db = connect(path)
    try:
        row = db.execute('SELECT updated,payload FROM snapshots WHERE target=? AND bot=?', (str(target), str(bot))).fetchone()
        if not row or not 0 <= time.time() - row[0] <= 90:
            return None
        payload = json.loads(row[1])
        return payload if payload.get('enabled') else None
    finally:
        db.close()
