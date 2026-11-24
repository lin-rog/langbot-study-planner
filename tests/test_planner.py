import asyncio
import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import Planner
from main import StudyPlanner


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo('Asia/Shanghai'))


def planner(state=None):
    return Planner(json.loads((ROOT / 'timetable.json').read_text(encoding='utf-8')), state)


class CoreTests(unittest.TestCase):
    def test_pruning_never_revives_acknowledged_cycle(self):
        p = planner()
        now = at('2026-10-11T22:00')
        p.reserve(p.jobs(now, {})[0], now)
        p.handle('!规划回应', now)
        p.prune(at('2026-10-30T12:00'))
        self.assertFalse(p.jobs(at('2026-10-30T12:00'), {}))

    def test_midnight_advance_and_zero_lead(self):
        p = planner()
        now = at('2026-10-11T21:00')
        p.submit(at('2026-10-12').date(), '00:05-00:30 阅读', now)
        self.assertEqual(p.jobs(at('2026-10-11T23:55'), {})[0]['kind'], 'plan')
        self.assertEqual(p.jobs(at('2026-10-12T00:05:10'), {'lead_minutes': 0})[0]['kind'], 'plan')

    def test_makeup_and_count(self):
        p = planner()
        self.assertEqual(len(p.timetable['events']), 7)
        self.assertEqual([len(p.classes(at(d).date())) for d in ('2026-10-17', '2026-10-24', '2026-10-31')], [3, 1, 3])

    def test_conflicts_and_atomic_replacement(self):
        p = planner()
        now = at('2026-10-16T21:00')
        day = now.date() + timedelta(days=1)
        p.submit(day, '18:00-19:00 学习', now)
        for text in ('10:25-11:00 学习', '18:00-20:00 A；19:00-21:00 B', '25:00-26:00 A', '22:00-01:00 A'):
            with self.assertRaises(ValueError):
                p.submit(day, text, now)
            self.assertEqual(p.state['plans'][str(day)]['items'][0]['title'], '学习')
        p.submit(day, '12:00-13:00 学习', now)
        with self.assertRaises(ValueError):
            p.submit(now.date(), '不另作安排', now)

    def test_nag_interval_negative_midnight_and_restart(self):
        p = planner()
        now = at('2026-10-11T22:00')
        job = p.jobs(now, {})[0]
        p.reserve(job, now)
        p = planner(json.loads(json.dumps(p.state)))
        self.assertFalse(p.jobs(now + timedelta(seconds=119), {}))
        self.assertEqual(p.jobs(now + timedelta(seconds=120), {})[0]['kind'], 'nag')
        signal = p.jobs(now + timedelta(minutes=10), {})[0]
        self.assertEqual(signal['tone'], 'impatient_teasing')
        self.assertEqual(signal['waiting_seconds'], 600)
        self.assertNotIn('text', signal)
        self.assertTrue(p.jobs(at('2026-10-12T00:00'), {}))
        self.assertEqual(p.jobs(at('2026-10-12T22:00'), {})[0]['date'], '2026-10-12')
        p.handle('!规划回应', at('2026-10-12T00:01'))
        self.assertFalse(p.jobs(at('2026-10-12T00:02'), {}))
        self.assertFalse(planner().jobs(at('2026-12-20T22:00'), {}))

    def test_explicit_ack_not_chatter(self):
        p = planner()
        now = at('2026-10-11T22:00')
        self.assertIsNone(p.handle('好的', now))
        p.handle('我在规划', now)
        self.assertFalse(p.jobs(now, {}))
        self.assertNotIn('2026-10-12', p.state['plans'])
        self.assertTrue(p.jobs(at('2026-10-12T22:00'), {}))
        p.handle('!暂停催促', now)
        self.assertFalse(p.jobs(at('2026-10-12T22:00'), {}))

    def test_empty_plan_and_event_dedup(self):
        p = planner()
        p.handle('!规划 不另作安排', at('2026-10-11T21:00'))
        self.assertFalse(p.jobs(at('2026-10-11T22:00'), {}))
        now = at('2026-10-17T10:15')
        job = p.jobs(now, {})[0]
        self.assertEqual(job['kind'], 'class')
        p.reserve(job, now)
        self.assertFalse(planner(json.loads(json.dumps(p.state))).jobs(now, {}))
        self.assertFalse(p.jobs(at('2026-10-17T10:26'), {}))


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def instance(self):
        obj = StudyPlanner()
        obj.planner = planner()
        obj.lock = asyncio.Lock()
        obj.tick_lock = asyncio.Lock()
        self.clock = at('2026-10-11T22:00')
        obj.now = lambda: self.clock
        self.cfg = dict(enabled=True, target_user_id='123', bot_uuid='bot', model_uuid='model', pipeline_uuid='example-pipeline')
        obj.cfg = lambda: self.cfg
        async def bot_info(bot):
            return {'enable': True, 'use_pipeline_uuid': 'example-pipeline'}
        obj.get_bot_info = bot_info
        obj.planner.state['context_pipeline_uuid'] = 'example-pipeline'
        obj.planner.state['persona_prompt'] = ['Use Japanese for study reminders.']
        obj.planner.state['dialogue_tail'] = [
            {'role': 'user', 'content': 'まだ明日の学習予定を考えていない。'},
            {'role': 'assistant', 'content': '決まったら教えてね。'}]
        self.requests = []
        async def invoke(**kwargs):
            from types import SimpleNamespace
            self.requests.append(kwargs)
            return SimpleNamespace(content='*明日の予定はもう考えた？')
        obj.invoke_llm = invoke
        self.saved = []
        async def save():
            self.saved.append(json.loads(json.dumps(obj.planner.state)))
        obj.save = save
        self.sent = []
        async def send(*args):
            self.sent.append(args)
        obj.send_message = send
        return obj

    async def test_sdk_send_and_restart(self):
        obj = self.instance()
        await obj.tick()
        await obj.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][:3], ('bot', 'person', '123'))
        self.assertEqual(next(iter(self.saved[0]['delivery'].values()))['status'], 'reserved')
        obj.planner = planner(self.saved[-1])
        self.clock += timedelta(minutes=2)
        await obj.tick()
        self.assertEqual(len(self.sent), 2)

    async def test_reply_during_generation_prevents_send(self):
        obj = self.instance()
        async def render(job):
            await obj.command('!规划回应')
            return '*明日の予定はもう考えた？'
        obj.render_signal = render
        await obj.tick()
        self.assertFalse(self.sent)

    async def test_failure_no_immediate_retry_and_storage_fail_closed(self):
        obj = self.instance()
        async def fail(*args):
            raise OSError('simulated platform failure')
        obj.send_message = fail
        with self.assertLogs('langbot.study_planner', level='ERROR'):
            await obj.tick()
        await obj.tick()
        self.assertEqual(next(iter(obj.planner.state['delivery'].values()))['status'], 'uncertain')
        self.clock += timedelta(minutes=2)
        async def save_fail():
            raise OSError('simulated storage failure')
        obj.save = save_fail
        with self.assertRaises(OSError):
            await obj.tick()
        with self.assertLogs('langbot.study_planner', level='ERROR'):
            reply = await obj.command('!规划 不另作安排')
        self.assertIn('保存失败', reply)
        self.assertFalse(obj.planner.state['plans'])

    async def test_filter_and_disable(self):
        obj = self.instance()
        self.assertTrue(obj.matches('123', 'bot'))
        self.assertFalse(obj.matches('124', 'bot'))
        self.assertFalse(obj.matches('123', 'another'))
        self.cfg['enabled'] = False
        await obj.tick()
        self.assertFalse(self.sent)

    async def test_nag_reaches_persona_model_as_signal(self):
        obj = self.instance()
        await obj.tick()
        self.assertEqual(len(self.requests), 1)
        messages = self.requests[0]['messages']
        self.assertIn('Use Japanese', messages[0].content)
        signal = json.loads(messages[-1].content)
        self.assertEqual(signal['signal'], 'next_day_plan_missing')
        self.assertEqual(signal['tone'], 'gentle')
        self.assertNotIn('text', signal)
        self.assertEqual(self.sent[0][3][0].text, '*明日の予定はもう考えた？')
        self.clock += timedelta(minutes=10)
        await obj.tick()
        signal = json.loads(self.requests[-1]['messages'][-1].content)
        self.assertEqual(signal['tone'], 'impatient_teasing')
        self.assertTrue(any(m.role == 'assistant' for m in self.requests[-1]['messages']))

    async def test_generation_failure_never_sends_template_and_backs_off(self):
        obj = self.instance()
        calls = []
        async def fail(**kwargs):
            calls.append(kwargs)
            raise OSError('simulated model outage')
        obj.invoke_llm = fail
        with self.assertLogs('langbot.study_planner', level='ERROR'):
            await obj.tick()
        self.clock += timedelta(seconds=119)
        await obj.tick()
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.sent)
        self.assertFalse(obj.planner.state['nag'])
        self.clock += timedelta(seconds=1)
        with self.assertLogs('langbot.study_planner', level='ERROR'):
            await obj.tick()
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.sent)

    async def test_missing_model_never_sends_fixed_nag(self):
        obj = self.instance()
        self.cfg.pop('model_uuid')
        with self.assertLogs('langbot.study_planner', level='WARNING'):
            await obj.tick()
        self.assertFalse(self.sent)
        self.assertFalse(obj.planner.state['nag'])

    async def test_current_private_dialogue_not_wrong_nags_is_model_history(self):
        obj = self.instance()
        obj.planner.state['recent_reminders'] = [dict(date='2026-10-12', kind='nag', text='Wrong meeting story')]
        await obj.tick()
        messages = self.requests[0]['messages']
        self.assertTrue(any(m.role == 'user' and '学習予定' in m.content for m in messages))
        self.assertFalse(any(m.role == 'assistant' and 'Wrong meeting' in m.content for m in messages))

    async def test_new_user_turn_during_generation_discards_stale_nag(self):
        obj = self.instance()
        async def render(job):
            await obj.record_user_turn('明天下午6点去图书馆看书，8点半回')
            return '*明日の予定は？'
        obj.render_signal = render
        await obj.tick()
        self.assertFalse(self.sent)
        self.assertFalse(obj.planner.state['nag'])

    async def test_japanese_persona_rejects_chinese_generation(self):
        from types import SimpleNamespace
        obj = self.instance()
        obj.planner.state['persona_prompt'] = ['必ず日本語で返答する。']
        async def invoke(**kwargs):
            return SimpleNamespace(content='*明天在哪儿碰头？')
        obj.invoke_llm = invoke
        with self.assertLogs('langbot.study_planner', level='WARNING'):
            await obj.tick()
        self.assertFalse(self.sent)

    async def test_completed_reply_captures_same_conversation_only(self):
        obj = self.instance()
        await obj.record_user_turn('明天下午6点去图书馆看书，8点半回')
        await obj.record_reply('明日の六時から図書館で勉強ね。')
        self.assertEqual(obj.planner.state['dialogue_tail'][-2]['content'], '明天下午6点去图书馆看书，8点半回')
        self.assertNotIn('reply_started_at', obj.planner.state)

    async def test_delivered_nag_visible_to_ordinary_reply_without_memory_injection(self):
        obj = self.instance()
        await obj.tick()
        prompt = []
        obj.inject_dialogue(prompt)
        self.assertEqual(prompt[-1].role, 'assistant')
        self.assertEqual(prompt[-1].content, self.sent[0][3][0].text)
        self.assertFalse(any(m.role == 'system' for m in prompt))

    async def test_capture_keeps_only_primary_persona_and_filters_recipient(self):
        from langbot_plugin.api.entities.builtin.provider.message import Message
        obj = self.instance()
        prompts = [Message(role='system', content='The new persona'),
                   Message(role='system', content='Private injected memory context')]
        await obj.capture_persona('other', 'bot', prompts)
        self.assertIn('Use Japanese', obj.planner.state['persona_prompt'][0])
        await obj.capture_persona('123', 'bot', prompts)
        self.assertEqual(obj.planner.state['persona_prompt'], ['The new persona'])

    async def test_listener_blocks_default_and_postorder(self):
        obj = self.instance()
        path = ROOT / 'components/event_listener/default.py'
        spec = importlib.util.spec_from_file_location('listener_under_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain
        from types import SimpleNamespace
        listener = module.StudyListener()
        listener.plugin = obj
        handlers = []
        def handler(event):
            def register(fn):
                handlers.append(fn)
                return fn
            return register
        listener.handler = handler
        await listener.initialize()
        class Context:
            event = SimpleNamespace(sender_id='123', message_chain=MessageChain([Plain(text='!规划 不另作安排')]))
            blocked = []
            async def get_bot_uuid(self):
                return 'bot'
            async def get_query_vars(self):
                return {'_pipeline_bound_plugins': ['local/study-planner']}
            def prevent_default(self):
                self.blocked.append('default')
            def prevent_postorder(self):
                self.blocked.append('postorder')
            async def reply(self, chain):
                self.reply_chain = chain
        ctx = Context()
        await handlers[0](ctx)
        self.assertEqual(ctx.blocked, ['default', 'postorder'])
        self.assertIn('已保存', ctx.reply_chain[0].text)

    async def test_other_active_pipeline_stops_generation_and_preserves_plans(self):
        obj = self.instance()
        obj.planner.state['plans']['2026-10-12'] = [{'start': '18:00', 'end': '19:00', 'text': '看书'}]
        before = json.loads(json.dumps(obj.planner.state))
        async def switched(bot):
            return {'enable': True, 'use_pipeline_uuid': 'fus'}
        obj.get_bot_info = switched
        await obj.tick()
        self.assertFalse(self.requests)
        self.assertFalse(self.sent)
        self.assertEqual(obj.planner.state, before)

    async def test_switch_during_generation_discards_old_pipeline_message(self):
        obj = self.instance()
        async def render(job):
            async def switched(bot):
                return {'enable': True, 'use_pipeline_uuid': 'fus'}
            obj.get_bot_info = switched
            return '*明日の予定は？'
        obj.render_signal = render
        await obj.tick()
        self.assertFalse(self.sent)
        self.assertFalse(obj.planner.state['delivery'])
        self.assertFalse(obj.planner.state['nag'])

    async def test_switch_during_reservation_does_not_consume_nag(self):
        obj = self.instance()
        values = iter(['example-pipeline', 'example-pipeline', 'fus'])
        async def route(bot):
            return {'enable': True, 'use_pipeline_uuid': next(values)}
        obj.get_bot_info = route
        await obj.tick()
        self.assertFalse(self.sent)
        self.assertFalse(obj.planner.state['delivery'])
        self.assertFalse(obj.planner.state['nag'])

    async def test_switch_back_resumes_without_losing_plan_state(self):
        obj = self.instance()
        active = 'fus'
        async def route(bot):
            return {'enable': True, 'use_pipeline_uuid': active}
        obj.get_bot_info = route
        await obj.tick()
        self.assertFalse(self.sent)
        active = 'example-pipeline'
        await obj.tick()
        self.assertEqual(len(self.sent), 1)

    async def test_unknown_route_or_missing_scope_fails_closed(self):
        obj = self.instance()
        self.cfg.pop('pipeline_uuid')
        await obj.tick()
        self.assertFalse(self.sent)
        self.cfg['pipeline_uuid'] = 'example-pipeline'
        async def offline(bot):
            raise OSError('SDK unavailable')
        obj.get_bot_info = offline
        with self.assertLogs('langbot.study_planner', level='WARNING'):
            await obj.tick()
        self.assertFalse(self.requests)

    async def test_binding_is_required_for_event_scope(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        obj = self.instance()
        ctx = SimpleNamespace(get_query_vars=AsyncMock(return_value={'_pipeline_bound_plugins': ['local/fus-output']}))
        self.assertFalse(await obj.in_pipeline(ctx))
        ctx.get_query_vars.return_value = {'_pipeline_bound_plugins': ['local/study-planner']}
        self.assertTrue(await obj.in_pipeline(ctx))

    async def test_changing_configured_scope_discards_only_dialogue_cache(self):
        obj = self.instance()
        obj.planner.state['plans']['2026-10-12'] = ['saved plan']
        self.cfg['pipeline_uuid'] = 'new-pipeline'
        await obj.ensure_context_scope()
        self.assertNotIn('persona_prompt', obj.planner.state)
        self.assertNotIn('dialogue_tail', obj.planner.state)
        self.assertEqual(obj.planner.state['plans']['2026-10-12'], ['saved plan'])

    async def test_other_pipeline_listener_does_not_capture_or_handle_anything(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain
        obj = self.instance()
        path = ROOT / 'components/event_listener/default.py'
        spec = importlib.util.spec_from_file_location('isolated_listener', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        listener = module.StudyListener()
        listener.plugin = obj
        handlers = []
        listener.handler = lambda event: lambda fn: handlers.append(fn) or fn
        await listener.initialize()
        obj.record_user_turn = AsyncMock()
        obj.command = AsyncMock()
        ctx = SimpleNamespace(event=SimpleNamespace(sender_id='123', message_chain=MessageChain([Plain(text='!规划 不另作安排')])),
            get_bot_uuid=AsyncMock(return_value='bot'), get_query_vars=AsyncMock(return_value={'_pipeline_bound_plugins': ['local/fus-output']}))
        await handlers[0](ctx)
        obj.record_user_turn.assert_not_awaited()
        obj.command.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
