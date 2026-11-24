import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from day_rules import expire_studies
from main import AppointmentReminder


class DayBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_midnight_during_render_blocks_outbound_delivery(self):
        clock = datetime(2026, 11, 4, 23, 59, 59, tzinfo=timezone.utc)
        study = {
            'id': 'study', 'status': 'pending', 'study_plan': True,
            'due_at': '2026-11-04T23:59:00+00:00',
        }
        ordinary = {
            'id': 'ordinary', 'status': 'pending', 'study_plan': False,
            'due_at': '2026-11-04T23:59:00+00:00',
        }
        items = [study, ordinary]
        send = AsyncMock()

        async def save():
            expire_studies(items, clock)

        async def render(_item, _now):
            nonlocal clock
            clock = datetime(2026, 11, 5, 0, 0, 1, tzinfo=timezone.utc)
            return '*示例提醒'

        reminder = SimpleNamespace(
            _state={'reminders': items}, _lock=asyncio.Lock(), _send_lock=asyncio.Lock(),
            now=lambda: clock, cfg=lambda: {'enabled': True, 'target_user_id': 'example', 'bot_uuid': 'example'},
            _save_locked=save, _render_due=render, send_message=send,
        )
        with patch('main.publish'):
            await AppointmentReminder._tick(reminder)
        send.assert_not_awaited()
        self.assertEqual(study['status'], 'expired')
        self.assertEqual(ordinary['status'], 'pending')
