"""Cover screenshot regression, ambiguous references and durable reminder delivery."""
import asyncio
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from references import resolve, is_reference
from main import AppointmentReminder

NOW = datetime(2026, 10, 13, 10, 45, 20, tzinfo=ZoneInfo('Asia/Shanghai'))
REQUEST = '*わかった、その時間になったら呼んでね。'
ORIGINAL = '*午前の授業は12時10分に終わるよ。霖も図書館で頑張ってね。お昼、よかったら一緒に食べようか。'
HISTORY = [{'role': 'assistant', 'content': ORIGINAL}]


class ReferenceTests(unittest.TestCase):
    def resolve(self, text=REQUEST, history=HISTORY, age=10, now=NOW):
        return resolve(text, history, (now-timedelta(seconds=age)).isoformat(), now)

    def test_screenshot_resolves_noon_not_midnight(self):
        result = self.resolve()
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['due_at'].isoformat(), '2026-10-13T12:10:00+08:00')

    def test_chinese_reference(self):
        self.assertEqual(self.resolve('到那个时间叫我。')['status'], 'resolved')
        self.assertEqual(self.resolve('到时候提醒我。')['status'], 'resolved')

    def test_japanese_reference_to_end(self):
        self.assertEqual(self.resolve('*終わったら知らせてね。')['status'], 'resolved')

    def test_plain_agreement_is_not_a_new_reminder(self):
        self.assertFalse(is_reference('*わかった、頑張ってね。'))

    def test_conflicting_clocks_require_clarification(self):
        result = self.resolve(history=[{'role': 'assistant', 'content': '12時10分か13時に会えるよ。'}])
        self.assertEqual(result['status'], 'ambiguous')

    def test_latest_assistant_only_no_old_time_scanning(self):
        result = self.resolve(history=HISTORY + [{'role': 'assistant', 'content': 'お昼に会おう。'}])
        self.assertEqual(result['status'], 'ambiguous')

    def test_stale_context_is_rejected(self):
        self.assertEqual(self.resolve(age=8000)['status'], 'ambiguous')

    def test_no_context_after_reset(self):
        self.assertEqual(self.resolve(history=[])['status'], 'ambiguous')

    def test_explicit_afternoon_clock(self):
        result = self.resolve(history=[{'role': 'assistant', 'content': '午後2時半に会おう。'}])
        self.assertEqual(result['due_at'].strftime('%H:%M'), '14:30')

    def test_information_question_and_negative_request_are_not_reminders(self):
        self.assertFalse(is_reference('*その時間を教えて。'))
        self.assertFalse(is_reference('到时候不要叫我。'))
        self.assertFalse(is_reference('*その時間になったら呼んでくれなくていい。'))

    def test_unresolved_calendar_day_is_not_assumed_today(self):
        self.assertEqual(self.resolve(history=[{'role': 'assistant', 'content': '来週12時10分に会おう。'}])['status'], 'ambiguous')

    def test_kanji_time(self):
        result = self.resolve(history=[{'role': 'assistant', 'content': '十二時十分に授業が終わる。'}])
        self.assertEqual(result['due_at'].strftime('%H:%M'), '12:10')

    def test_explicit_user_tomorrow_override(self):
        result = self.resolve('*明日、その時間になったら呼んでね。')
        self.assertEqual(result['due_at'].date().isoformat(), '2026-10-14')

    def test_elapsed_time_is_not_rolled_to_tomorrow(self):
        result = self.resolve(now=NOW.replace(hour=13))
        self.assertEqual(result['status'], 'elapsed')
        self.assertEqual(result['due_at'].date().isoformat(), '2026-10-13')

    def test_analysis_output_is_not_reference_context(self):
        result = self.resolve(history=[{'role': 'assistant', 'content': '【读音补充】12時10分に終わるよ。'}])
        self.assertEqual(result['status'], 'ambiguous')


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def listener(self):
        path = Path(__file__).resolve().parents[1] / 'components/event_listener/default.py'
        spec = importlib.util.spec_from_file_location('appointment_listener_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        listener = module.AppointmentReminderListener()
        listener.plugin = self.obj
        handlers = {}
        listener.handler = lambda event: lambda fn: handlers.setdefault(event.__name__, fn) or fn
        await listener.initialize()
        return handlers

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.obj = AppointmentReminder()
        self.obj._lock = asyncio.Lock()
        self.obj._send_lock = asyncio.Lock()
        self.obj._pending_candidates = {}
        self.obj._reference_questions = set()
        self.obj._inspection_tasks = set()
        self.obj._last_user_turn = {}
        self.obj._state = {'prompt_context': {'history': HISTORY, 'captured_at': (NOW-timedelta(seconds=10)).isoformat()}, 'reminders': [], 'bot_uuid': 'bot'}
        self.obj._legacy_path = Path(self.temp.name) / 'state.json'
        self.obj._storage_available = False
        self.obj.get_config = lambda: {'enabled': True, 'natural_detection_enabled': True, 'target_user_id': 'person', 'bot_uuid': 'bot', 'timezone': 'Asia/Shanghai'}
        self.obj.now = lambda: NOW
        self.obj._export_study = lambda: None

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_reference_creates_durable_non_study_reminder_before_reply(self):
        self.obj.remember_candidate('person', REQUEST)
        self.assertTrue(await self.obj.resolve_incoming_reference('person', REQUEST))
        saved = json.loads(self.obj._legacy_path.read_text(encoding='utf-8'))
        item = saved['reminders'][0]
        self.assertEqual(item['due_at'], '2026-10-13T12:10:00+08:00')
        self.assertEqual(item['evidence'], REQUEST)
        self.assertFalse(item['study_plan'])
        self.assertEqual(self.obj.take_candidate('person'), '')

    async def test_duplicate_reference_does_not_create_duplicate_send(self):
        await self.obj.resolve_incoming_reference('person', REQUEST)
        await self.obj.resolve_incoming_reference('person', REQUEST)
        self.assertEqual(len(self.obj._state['reminders']), 1)

    async def test_ambiguity_instructs_clarification_without_registering(self):
        self.obj._state['prompt_context']['history'] = [{'role': 'assistant', 'content': '12:10か13:00ね。'}]
        await self.obj.resolve_incoming_reference('person', REQUEST)
        self.assertTrue(self.obj.needs_reference_question('person'))
        self.assertFalse(self.obj._state['reminders'])

    async def test_reset_clears_reference_history_but_keeps_appointments(self):
        await self.obj.resolve_incoming_reference('person', REQUEST)
        await self.obj.clear_conversation_reference('person')
        self.assertEqual(len(self.obj._state['reminders']), 1)
        self.assertEqual(self.obj._state['prompt_context']['history'], [])

    async def test_non_candidate_input_cannot_reuse_an_old_request(self):
        self.obj.remember_candidate('person', REQUEST)
        self.obj.remember_candidate('person', '今日は暑いね。')
        self.assertEqual(self.obj.take_candidate('person'), '')

    async def test_other_recipient_cannot_register_reference(self):
        self.assertFalse(await self.obj.resolve_incoming_reference('other', REQUEST))
        self.assertFalse(self.obj._state['reminders'])

    async def test_due_message_sends_once_using_persona_signal(self):
        await self.obj.resolve_incoming_reference('person', REQUEST)
        self.obj.now = lambda: NOW.replace(hour=12, minute=10, second=0)
        self.obj._render_due = AsyncMock(return_value='*霖、授業が終わったよ。お昼、一緒に食べようか。')
        self.obj.send_message = AsyncMock()
        with patch('main.publish'):
            await self.obj._tick()
            await self.obj._tick()
        self.obj.send_message.assert_awaited_once()
        self.assertEqual(self.obj._state['reminders'][0]['status'], 'delivered')

    async def test_background_model_inspection_does_not_block_reply(self):
        gate = asyncio.Event()
        async def slow(*args):
            await gate.wait()
        self.obj.inspect_completed_conversation = AsyncMock(side_effect=slow)
        self.obj.queue_inspection('person', HISTORY, '明日12:00提醒我吃饭')
        await asyncio.sleep(0)
        self.assertTrue(self.obj._inspection_tasks)
        gate.set()
        await asyncio.gather(*self.obj._inspection_tasks)

    async def test_private_message_listener_saves_screenshot_request_without_blocking_chat(self):
        from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain
        handlers = await self.listener()
        ctx = SimpleNamespace(event=SimpleNamespace(sender_id='person', message_chain=MessageChain([Plain(text=REQUEST)])),
            get_bot_uuid=AsyncMock(return_value='bot'), prevent_default=lambda: self.fail('Character chat must continue'),
            prevent_postorder=lambda: self.fail('Character chat must continue'))
        await handlers['PersonMessageReceived'](ctx)
        self.assertEqual(self.obj._state['reminders'][0]['due_at'], '2026-10-13T12:10:00+08:00')

    async def test_newly_generated_time_reply_is_available_to_next_reference_request(self):
        self.obj._state['prompt_context']['history'] = []
        self.obj.remember_user_turn('person', '午前の授業はいつ終わる？')
        handlers = await self.listener()
        ctx = SimpleNamespace(event=SimpleNamespace(sender_id='person', launcher_type='person', funcs_called=[],
            response_text=ORIGINAL, session=SimpleNamespace(using_conversation=None)))
        await handlers['NormalMessageResponded'](ctx)
        self.assertTrue(await self.obj.resolve_incoming_reference('person', REQUEST))
        self.assertEqual(self.obj._state['reminders'][0]['due_at'], '2026-10-13T12:10:00+08:00')

    async def test_ambiguous_reference_injects_persona_question_before_normal_reply(self):
        from langbot_plugin.api.entities.builtin.provider.message import Message
        self.obj._state['prompt_context']['history'] = [{'role': 'assistant', 'content': '12:10か13:00ね。'}]
        await self.obj.resolve_incoming_reference('person', REQUEST)
        handlers = await self.listener()
        ctx = SimpleNamespace(event=SimpleNamespace(default_prompt=[], prompt=[Message(role='assistant', content='12:10か13:00ね。')]),
            get_query_vars=AsyncMock(return_value={'sender_id': 'person'}), get_bot_uuid=AsyncMock(return_value='bot'))
        await handlers['PromptPreProcessing'](ctx)
        self.assertIn('问清具体时间', ctx.event.prompt[-1].content)
        self.assertFalse(self.obj._state['reminders'])


if __name__ == '__main__':
    unittest.main()
