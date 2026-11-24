import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main import StudyPlanner
from core import Planner
from study_bridge import publish, snapshot


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'bridge.db')
        self.now = datetime(2026, 10, 11, 22, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.item = dict(id='one', due_at='2026-10-12T20:00:00+08:00', summary='复习Python',
                         created_at=self.now.isoformat(), status='pending', study_plan=True)

    def study(self):
        obj = StudyPlanner()
        obj.planner = Planner(json.loads((ROOT / 'timetable.json').read_text(encoding='utf-8')))
        obj.lock = asyncio.Lock()
        obj.tick_lock = asyncio.Lock()
        obj.cfg = lambda: dict(enabled=True, appointment_bridge_enabled=True, target_user_id='123',
                              bot_uuid='bot', bridge_db_path=self.path, pipeline_uuid='kihara')
        obj.planner.state['context_pipeline_uuid'] = 'kihara'
        async def route(bot):
            return {'enable': True, 'use_pipeline_uuid': 'kihara'}
        obj.get_bot_info = route
        obj.now = lambda: self.now
        return obj

    async def test_scope_cancel_disabled_stale_and_missing(self):
        obj = self.study()
        self.assertFalse(obj.sync_appointments())
        publish('other', 'bot', [self.item], True, self.path)
        self.assertFalse(obj.sync_appointments())
        publish('123', 'bot', [self.item], True, self.path)
        self.assertTrue(obj.sync_appointments())
        self.assertEqual(obj.planner.external_days, {'2026-10-12'})
        self.assertFalse(obj.planner.jobs(self.now, {}))
        publish('123', 'bot', [dict(self.item, status='cancelled')], True, self.path)
        obj.sync_appointments()
        self.assertTrue(obj.planner.jobs(self.now, {}))
        publish('123', 'bot', [self.item], False, self.path)
        self.assertFalse(obj.sync_appointments())
        publish('123', 'bot', [self.item], True, self.path)
        with patch('study_bridge.time.time', return_value=time.time()+100):
            self.assertIsNone(snapshot('123', 'bot', self.path))

    async def test_non_learning_and_not_prebooked_never_count(self):
        obj = self.study()
        publish('123', 'bot', [dict(self.item, study_plan=False),
                               dict(self.item, created_at='2026-10-10T22:00:00+08:00')], True, self.path)
        obj.sync_appointments()
        self.assertFalse(obj.planner.external_days)

    async def test_bridge_absence_suppresses_nags(self):
        obj = self.study()
        sent = []
        async def send(*args):
            sent.append(args)
        obj.send_message = send
        await obj.tick()
        self.assertFalse(sent)

    async def test_natural_control_no_task_registration(self):
        obj = self.study()
        async def save():
            pass
        obj.save = save
        obj.planner.reserve(obj.planner.jobs(self.now, {})[0], self.now)
        reply = await obj.natural_control('先别催，我正在安排')
        self.assertIn('已停止', reply)
        self.assertFalse(obj.planner.state['plans'])
        self.assertIsNone(await obj.natural_control('明天晚上八点提醒我复习Python'))

    async def test_real_appointment_natural_extractor_and_snapshot(self):
        bridge_root = ROOT / 'appointment-reminder-study-bridge'
        sys.path.insert(0, str(bridge_root))
        spec = importlib.util.spec_from_file_location('appointment_test', bridge_root / 'main.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        obj = module.AppointmentReminder()
        obj._lock = asyncio.Lock()
        obj._state = {}
        obj.cfg = lambda: dict(enabled=True, model_uuid='model', study_bridge_enabled=True,
                              target_user_id='123', bot_uuid='bot', study_bridge_db_path=self.path)
        obj.now = lambda: self.now
        async def save():
            obj._export_study()
        obj._save_locked = save
        user = '明天晚上八点提醒我复习Python'
        self.assertTrue(obj.is_candidate(user))
        self.assertTrue(obj.is_candidate('明天下午6点去图书馆看书，8点半回'))
        self.assertTrue(obj.is_candidate('明日18時に図書館で読書する'))
        self.assertFalse(obj.is_candidate('这本书看起来不错'))
        async def invoke(*args):
            return json.dumps(dict(accepted=True, due_at=self.item['due_at'], summary='复习Python',
                                   study_plan=True, evidence=user), ensure_ascii=False)
        obj._invoke = invoke
        await obj.inspect_completed_conversation('123', [{'role': 'user', 'content': user}], user)
        self.assertEqual(len(obj._state['reminders']), 1)
        obj._export_study()
        study = self.study()
        self.assertTrue(study.sync_appointments())
        self.assertEqual(study.planner.external_days, {'2026-10-12'})
        self.assertFalse(study.planner.jobs(self.now, {}))
        await obj.inspect_completed_conversation('123', [{'role': 'user', 'content': user}], user)
        self.assertEqual(len(obj._state['reminders']), 1)
        await obj.cancel(obj._state['reminders'][0]['id'])
        study.sync_appointments()
        self.assertFalse(study.planner.external_days)
