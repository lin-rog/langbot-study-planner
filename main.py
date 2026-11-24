"""LangBot adapter: isolated reminder signals and private-message delivery."""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from langbot_plugin.api.definition.plugin import BasePlugin
from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain
from langbot_plugin.api.entities.builtin.provider.message import Message
from core import Planner
from study_bridge import snapshot

LOGGER = logging.getLogger('langbot.study_planner')
STATE_KEY = 'study_planner_state_v1.json'


def selector(value):
    return str(value.get('uuid') or value.get('id') or '') if isinstance(value, dict) else str(value or '')


class StudyPlanner(BasePlugin):
    async def initialize(self):
        self.lock = asyncio.Lock()
        self.tick_lock = asyncio.Lock()
        self.local = Path(__file__).with_name('state.json')
        self.storage = True
        try:
            raw = await self.get_plugin_storage(STATE_KEY)
        except Exception:
            LOGGER.warning('SDK storage unavailable; using atomic local storage')
            self.storage = False
            raw = self.local.read_bytes() if self.local.exists() else b''
        # Corrupt data must fail visibly, rather than reset acknowledgements.
        state = json.loads(raw) if raw else {}
        path = Path(self.cfg().get('timetable_path') or Path(__file__).with_name('timetable.json'))
        self.planner = Planner(json.loads(path.read_text(encoding='utf-8')), state)
        await self.ensure_context_scope()
        self.now()  # Validate the configured timezone before starting the timer.
        self.task = asyncio.create_task(self.scheduler(), name='study-planner')

    def cfg(self):
        return self.get_config() or {}

    def now(self):
        return datetime.now(ZoneInfo(self.cfg().get('timezone') or 'Asia/Shanghai'))

    def matches(self, sender, bot):
        cfg = self.cfg()
        return (str(sender) == str(cfg.get('target_user_id') or '') and bool(str(sender))
                and bool(selector(cfg.get('bot_uuid'))) and str(bot) == selector(cfg.get('bot_uuid')))

    async def pipeline_active(self, cfg=None):
        """Timers have no query event: inspect the bot's current persisted route."""
        cfg = cfg or self.cfg()
        expected = selector(cfg.get('pipeline_uuid'))
        if not expected:
            return False
        try:
            info = await asyncio.wait_for(self.get_bot_info(selector(cfg.get('bot_uuid'))), timeout=5)
            active = bool(info.get('enable')) and str(info.get('use_pipeline_uuid') or '') == expected
            self._route_error_reported = False
            return active
        except Exception:
            if not getattr(self, '_route_error_reported', False):
                LOGGER.warning('Could not verify the active pipeline; study planner remains inactive', exc_info=True)
                self._route_error_reported = True
            return False

    async def in_pipeline(self, ctx):
        """Respect the event's explicit plugin binding as well as the bot route."""
        try:
            variables = await ctx.get_query_vars()
            bound = variables.get('_pipeline_bound_plugins')
            return isinstance(bound, list) and 'local/study-planner' in bound and await self.pipeline_active()
        except Exception:
            return False

    async def ensure_context_scope(self):
        expected = selector(self.cfg().get('pipeline_uuid'))
        if not expected:
            return
        previous = self.planner.state.get('context_pipeline_uuid')
        if previous == expected:
            return
        async with self.lock:
            if previous:
                for key in ('persona_prompt', 'dialogue_tail', 'recent_reminders', 'pending_user_turn', 'reply_started_at'):
                    self.planner.state.pop(key, None)
                self.planner.state['dialogue_revision'] = self.planner.state.get('dialogue_revision', 0) + 1
            self.planner.state['context_pipeline_uuid'] = expected
            await self.save()

    async def save(self):
        data = json.dumps(self.planner.state, ensure_ascii=False).encode('utf-8')
        if self.storage:
            await self.set_plugin_storage(STATE_KEY, data)
        else:
            temporary = self.local.with_suffix('.json.tmp')
            with temporary.open('wb') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.local)

    async def capture_persona(self, sender, bot, default_prompt):
        """Copy only the static persona; do not collect conversation or memory."""
        if not self.matches(sender, bot):
            return
        persona = []
        for item in default_prompt or []:
            role = getattr(item, 'role', '')
            if str(getattr(role, 'value', role)) != 'system':
                continue
            content = getattr(item, 'content', '')
            if not isinstance(content, str):
                content = ''.join(getattr(p, 'text', '') or '' for p in content or [])
            if content.strip():
                persona.append(content.strip())
                # Later injected system entries may contain memory, clock, or life state.
                break
        if persona:
            async with self.lock:
                if persona != self.planner.state.get('persona_prompt'):
                    self.planner.state['persona_prompt'] = persona
                    await self.save()

    async def record_user_turn(self, text):
        """Invalidate in-flight reminders when the private conversation advances."""
        async with self.lock:
            self.planner.state['dialogue_revision'] = self.planner.state.get('dialogue_revision', 0) + 1
            self.planner.state['pending_user_turn'] = text[:4000]
            self.planner.state['reply_started_at'] = self.now().isoformat()
            await self.save()

    async def record_reply(self, text):
        async with self.lock:
            pending = self.planner.state.pop('pending_user_turn', '')
            self.planner.state.pop('reply_started_at', None)
            history = self.planner.state.setdefault('dialogue_tail', [])
            if pending:
                history.append({'role': 'user', 'content': pending})
            if text:
                history.append({'role': 'assistant', 'content': text[:4000]})
            self.planner.state['dialogue_tail'] = history[-8:]
            self.planner.state['dialogue_updated_at'] = self.now().isoformat()
            await self.save()

    def inject_dialogue(self, prompt):
        """Expose real delivered private turns to the ordinary reply, not memory."""
        if not prompt:
            prompt.extend(Message(role=i['role'], content=i['content'])
                          for i in self.planner.state.get('dialogue_tail', [])[-8:]
                          if i.get('role') in ('user', 'assistant') and i.get('content'))
            return
        existing = {i.content for i in prompt if getattr(i, 'role', '') == 'assistant'
                    and isinstance(getattr(i, 'content', None), str)}
        for item in self.planner.state.get('recent_reminders', []):
            anchor = item.get('anchor')
            if not anchor or item['text'] in existing:
                continue
            matches = [n for n, i in enumerate(prompt)
                       if getattr(i, 'role', '') == anchor['role'] and getattr(i, 'content', None) == anchor['content']]
            if matches:
                prompt.insert(matches[-1] + 1, Message(role='assistant', content=item['text']))
                existing.add(item['text'])

    async def command(self, text):
        async with self.lock:
            self.sync_appointments()
            before = json.loads(json.dumps(self.planner.state))
            result = self.planner.handle(text, self.now())
            if result is not None:
                try:
                    await self.save()
                except Exception:
                    self.planner.state = before
                    LOGGER.exception('Plan persistence failed')
                    return '保存失败，未确认成功，请稍后重试。'
            return result

    def sync_appointments(self):
        cfg = self.cfg()
        if not cfg.get('appointment_bridge_enabled', False):
            self.planner.external_days = set()
            return True
        payload = snapshot(str(cfg.get('target_user_id') or ''), selector(cfg.get('bot_uuid')),
                           cfg.get('bridge_db_path') or None)
        if payload is None:
            self.planner.external_days = set()
            return False
        days = set()
        for item in payload['reminders']:
            if item.get('study_plan') is not True or item.get('status') not in ('pending', 'delivered'):
                continue
            due = datetime.fromisoformat(item['due_at']).astimezone(self.now().tzinfo)
            created = datetime.fromisoformat(item['created_at']).astimezone(self.now().tzinfo)
            if created.date() == due.date() - timedelta(days=1):
                days.add(str(due.date()))
        self.planner.external_days = days
        return True

    async def natural_control(self, text):
        # Ignore the phone-scene marker when classifying control phrases.
        text = text.strip()
        if text.startswith('*'):
            text = text[1:].lstrip()
        direct = {'明天有哪些课': '!明日课表', '明天的课表': '!明日课表',
                  '先别催，我正在安排': '!规划回应', '先别催': '!规划回应',
                  '今晚不用再催了': '!今晚跳过', '暂停催促': '!暂停催促', '恢复催促': '!恢复催促',
                  '明天不另作安排': '!规划 不另作安排'}
        if text.strip() in direct:
            return await self.command(direct[text.strip()])
        cfg = self.cfg()
        model = selector(cfg.get('model_uuid'))
        if not model or not cfg.get('natural_control_enabled', True) or len(text) > 2000:
            return None
        if not any(word in text for word in ('催', '课表', '不安排', '不另作', '规划')):
            return None
        try:
            result = await asyncio.wait_for(self.invoke_llm(llm_model_uuid=model, messages=[
                Message(role='system', content=(
                    '只分类当前用户直接表达的控制意图。输出JSON {"action":"..."}。'
                    '允许action:none,overview,ack,skip,pause,resume,no_extra。'
                    'overview=询问明天课表；ack=明确回复正在规划并要求停止催促；skip=本轮别催；'
                    'pause=长期停止催促；resume=恢复；no_extra=明确说明明天不另作安排。'
                    '时间任务的登记、修改、取消全归约定提醒，必须输出none。'
                    '引用、假设、否定控制意图、含糊应答必须none，不把“稍后安排/准备规划”当已完成。')),
                Message(role='user', content=text)], extra_args={'max_tokens': 100}), timeout=15)
            raw = result.content if isinstance(result.content, str) else ''
            action = json.loads(raw)['action']
            commands = {'overview': '!明日课表', 'ack': '!规划回应', 'skip': '!今晚跳过',
                        'pause': '!暂停催促', 'resume': '!恢复催促', 'no_extra': '!规划 不另作安排'}
            return await self.command(commands[action]) if action in commands else None
        except Exception:
            LOGGER.exception('Natural control classification failed; no state changed')
            return None

    async def render_signal(self, job):
        cfg = self.cfg()
        model = selector(cfg.get('model_uuid'))
        persona = self.planner.state.get('persona_prompt', [])
        if not model or not persona:
            LOGGER.warning('Reminder signal deferred: wording model or persona is unavailable')
            return None
        messages = [Message(role='system', content=p) for p in persona]
        japanese = any('日本語' in p for p in persona)
        dialogue = self.planner.state.get('dialogue_tail', [])[-8:]
        if job['kind'] == 'nag' and not dialogue:
            LOGGER.warning('Planning nudge deferred: no current private conversation context')
            return None
        messages += [Message(role=i['role'], content=i['content']) for i in dialogue
                     if i.get('role') in ('user', 'assistant') and i.get('content')]
        messages.append(Message(role='system', content=(
            '这是发送给现有人设的一次提醒信号，请以该角色的口吻、称呼和语言，'
            '自然发给对方一条手机私信。仅生成消息正文，先以一个半角*开头；'
            '不写神态、动作、旁白或包裹整句的引号。不说明信号、插件、JSON或状态机。'
            '不输出日期标签、标题、操作指南、命令或固定回复口令。'
            '不要复制字段原文或把通知改写成系统公告。不要代写对方的回应或猜测他没回复的原因。'
            '规划尚未确认时只自然询问安排或催他给一个明确答复，不声称已安排或已完成。'
            '不得替对方决定、登记、修改计划，不威胁自动安排，不虚构已经采取的行动。'
            '授课和任务提醒必须保留事项、时间地点的事实，不擅自更改，不虚构迟到或完成。'
            'gentle表示自然关心；impatient_teasing表示等待至少十分钟后，表达不满、调侃、'
            '稍强硬地催促，保持人设，不作人格贬低。感情通过语言表达。'
            '用目标日期和当前日期判断明天、今天或具体日期，不能把跨日的旧轮次说成明天。'
            '不要重复最近已发送提醒的措辞。' +
            str(cfg.get('reminder_instruction') or '沿用当前人设与语言，像本人发来一条自然的私聊提醒。'))))
        if japanese:
            messages.append(Message(role='system', content=(
                'これは直前までの同じ私信の続き。木原から霖への文章を必ず日本語で書く。'
                '先頭の*以外は自然な一〜二文だけ。動作・表情・見出し・コマンド説明は書かない。'
                '催促の対象は霖自身の翌日の学習計画であり、二人の待ち合わせではない。'
                '相手が既に話した時間・場所・予定を無視して同じことを尋ねない。'
                '沈黙を同意と扱わず、相手の計画を勝手に決めたり、そのように脅したりしない。'
                '既に具体的な学習予定を伝えている場合、未回答だと責める文章は生成しない。')))
        recent = [i for i in self.planner.state.get('recent_reminders', [])
                  if i.get('date') == job['date'] and i.get('kind') == job['kind']][-4:]
        signal = {k: v for k, v in job.items() if k not in ('id', 'text')}
        signal['current_time'] = self.now().isoformat()
        signal['recently_delivered'] = [i['text'] for i in recent]
        if job['kind'] == 'nag':
            signal['planning_scope'] = 'User-owned study activities; not a meeting with the character.'
        messages.append(Message(role='user', content=json.dumps(signal, ensure_ascii=False)))
        try:
            response = await asyncio.wait_for(self.invoke_llm(
                llm_model_uuid=model,
                messages=messages,
                extra_args={'max_tokens': 350, 'thinking': {'type': 'disabled'}}), timeout=25)
            content = response.content
            text = content if isinstance(content, str) else ''.join(getattr(p, 'text', '') or '' for p in content or [])
            text = text.strip()
            if not text.lstrip('*').strip():
                return None
            if japanese and not re.search(r'[ぁ-ゖァ-ヺ]', text):
                LOGGER.warning('Rejected reminder: Japanese persona received a non-Japanese reply')
                return None
            return '*' + text.lstrip('*').lstrip()[:1500]
        except Exception:
            LOGGER.exception('Reminder generation failed; defer without sending a fixed template')
            return None

    async def tick(self):
        async with self.tick_lock:
            cfg = dict(self.cfg())
            if not cfg.get('enabled', False) or not cfg.get('target_user_id') or not selector(cfg.get('bot_uuid')):
                return
            if not await self.pipeline_active(cfg):
                return
            await self.ensure_context_scope()
            async with self.lock:
                bridge_ready = self.sync_appointments()
                if not bridge_ready:
                    cfg = dict(cfg, nag_enabled=False)
                self.planner.prune(self.now())
                jobs = self.planner.jobs(self.now(), cfg)
            for job in jobs:
                async with self.lock:
                    started = self.planner.state.get('reply_started_at')
                    if started and (self.now() - datetime.fromisoformat(started)).total_seconds() < 180:
                        continue
                    revision = self.planner.state.get('dialogue_revision', 0)
                    retry = self.planner.state.get('wording_retry_after', {}).get(job['id'])
                    if retry and self.now() < datetime.fromisoformat(retry):
                        continue
                text = await self.render_signal(job)
                if not text:
                    async with self.lock:
                        retries = self.planner.state.setdefault('wording_retry_after', {})
                        retries[job['id']] = (self.now() + timedelta(seconds=120)).isoformat()
                        await self.save()
                    continue
                text = '*' + text.lstrip().lstrip('*').lstrip()
                async with self.lock:
                    # A plan or acknowledgement may arrive during generation.
                    fresh = self.cfg()
                    if not self.sync_appointments():
                        fresh = dict(fresh, nag_enabled=False)
                    if not fresh.get('enabled', False):
                        return
                    if selector(fresh.get('pipeline_uuid')) != selector(cfg.get('pipeline_uuid')) or not await self.pipeline_active(fresh):
                        return
                    if revision != self.planner.state.get('dialogue_revision', 0):
                        continue
                    if fresh.get('target_user_id') != cfg.get('target_user_id') or fresh.get('bot_uuid') != cfg.get('bot_uuid'):
                        return
                    valid = {j['id'] for j in self.planner.jobs(self.now(), fresh)}
                    if job['id'] not in valid:
                        continue
                    before_reservation = json.loads(json.dumps(self.planner.state))
                    self.planner.state.setdefault('wording_retry_after', {}).pop(job['id'], None)
                    self.planner.reserve(job, self.now())
                    # Persist reservation before sending: no crash-driven flood.
                    await self.save()
                    # The user may switch pipelines during persistence or model
                    # generation. Do not dispatch a completed old-pipeline signal.
                    if not await self.pipeline_active(fresh):
                        self.planner.state = before_reservation
                        await self.save()
                        return
                    try:
                        await asyncio.wait_for(self.send_message(selector(cfg['bot_uuid']), 'person',
                            str(cfg['target_user_id']), MessageChain([Plain(text=text)])), timeout=30)
                    except Exception:
                        self.planner.state['delivery'][job['id']]['status'] = 'uncertain'
                        LOGGER.exception('Send failed or timed out; no automatic duplicate retry')
                    else:
                        self.planner.state['delivery'][job['id']]['status'] = 'sent'
                        recent = self.planner.state.setdefault('recent_reminders', [])
                        history = self.planner.state.setdefault('dialogue_tail', [])
                        anchor = dict(history[-1]) if history else None
                        recent.append({'date': job['date'], 'kind': job['kind'], 'text': text, 'anchor': anchor})
                        self.planner.state['recent_reminders'] = recent[-6:]
                        history.append({'role': 'assistant', 'content': text})
                        self.planner.state['dialogue_tail'] = history[-8:]
                    await self.save()

    async def scheduler(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception('Study planner tick failed')
            await asyncio.sleep(10)

    async def shutdown(self):
        if getattr(self, 'task', None):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
