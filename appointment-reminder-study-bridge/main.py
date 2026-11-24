from __future__ import annotations
from behavior_bus import publish
from study_bridge import publish as publish_study_snapshot
from day_rules import active_appointments, expire_studies, expired_study

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langbot_plugin.api.definition.plugin import BasePlugin
from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain
from langbot_plugin.api.entities.builtin.provider.message import Message
from langbot_plugin.entities.io.actions.enums import PluginToRuntimeAction
from references import is_reference, resolve


LOGGER = logging.getLogger("langbot.plugin.appointment_reminder")
STATE_KEY = "appointment_reminder_state_v1.json"
TIME_PATTERN = re.compile(
    r"(今天|明天|后天|今晚|今早|上午|下午|晚上|凌晨|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"今日|明日|明後日|今夜|今朝|午前|午後|夕方|今晩|[月火水木金土日]曜日|"
    r"\d{1,4}[年/-]\d{1,2}|\d{1,2}月\d{1,2}[日号]?|\d{1,2}[点時:]|"
    r"\d+\s*(分钟|小时|天)后|\d+\s*(分|時間|日)後)"
)
COMMITMENT_PATTERN = re.compile(
    r"(提醒|约定|约好|说好|记得|别忘|到时候|届时|准时|叫我|喊我|"
    r"約束|リマインド|忘れない|忘れず|覚えて|知らせて|教えて|呼んで|起こして|"
    r"メッセージ.{0,12}(送って|送る|ください|お願い)|連絡.{0,12}(して|する|ください|お願い)|"
    r"送って.{0,12}(ください|くれる|お願い)|わかった|分かった)"
)
STUDY_PATTERN = re.compile(r'(复习|预习|学习|练习|作业|刷题|背单词|备考|看书|读书|阅读|自习|勉強|復習|予習|読書|本を読む)')


def _selector_uuid(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("uuid") or value.get("id") or "").strip()
    return str(value or "").strip()


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


def _dt(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
        if text:
            parts.append(str(text))
    return "".join(parts)


def _response_text(response: Message) -> str:
    return _content_text(response.content).strip()


def _strip_thinking(text: str) -> str:
    cleaned = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", text or "", flags=re.I | re.S)
    closing = re.search(r"</think\s*>", cleaned, flags=re.I)
    if closing:
        cleaned = cleaned[closing.end():]
    opening = re.search(r"<think\b[^>]*>", cleaned, flags=re.I)
    if opening:
        cleaned = cleaned[:opening.start()]
    return cleaned.strip()


def _extract_json(text: str) -> dict[str, Any] | None:
    cleaned = _strip_thinking(text)
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.I | re.S)
    candidate = fenced.group(1) if fenced else cleaned
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(candidate[start:end + 1])
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


class AppointmentReminder(BasePlugin):
    async def initialize(self) -> None:
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._legacy_path = Path(__file__).resolve().with_name("state.json")
        self._storage_available = True
        self._pending_candidates: dict[str, str] = {}
        self._inspection_tasks = set()
        self._reference_questions = set()
        self._last_user_turn = {}
        self._state = await self._load_state()
        if expire_studies(self._state.get("reminders", []), self.now()):
            await self._save_locked()
        self._export_study()
        self._task = asyncio.create_task(self._scheduler(), name="appointment-reminder-scheduler")

    async def shutdown(self) -> None:
        task = getattr(self, "_task", None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        jobs = list(getattr(self, '_inspection_tasks', set()))
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    def __del__(self) -> None:
        task = getattr(self, "_task", None)
        if task and not task.done():
            task.cancel()

    def cfg(self) -> dict[str, Any]:
        raw = self.get_config() or {}
        return raw if isinstance(raw, dict) else {}

    def tz(self) -> tzinfo:
        try:
            return ZoneInfo(str(self.cfg().get("timezone") or "Asia/Shanghai"))
        except ZoneInfoNotFoundError:
            return timezone(timedelta(hours=8), name="Asia/Shanghai")

    def now(self) -> datetime:
        return datetime.now(self.tz())

    def matches(self, sender_id: Any) -> bool:
        target = str(self.cfg().get("target_user_id") or "").strip()
        return bool(target and str(sender_id) == target)

    async def _load_state(self) -> dict[str, Any]:
        try:
            raw = await self.get_plugin_storage(STATE_KEY)
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception as exc:
            self._storage_available = False
            LOGGER.info("Plugin storage unavailable; using local state: %s", exc)
        try:
            value = json.loads(self._legacy_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    async def _save_locked(self) -> None:
        expire_studies(self._state.get("reminders", []), self.now())
        payload = json.dumps(self._state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self._storage_available:
            try:
                await self.set_plugin_storage(STATE_KEY, payload)
                self._export_study()
                return
            except Exception as exc:
                self._storage_available = False
                LOGGER.info("Plugin storage unavailable; using local state: %s", exc)
        temporary = self._legacy_path.with_suffix(".json.tmp")
        await asyncio.to_thread(temporary.write_bytes, payload)
        await asyncio.to_thread(temporary.replace, self._legacy_path)
        self._export_study()

    def _export_study(self):
        cfg = self.cfg()
        if not cfg.get('study_bridge_enabled', False):
            return
        try:
            publish_study_snapshot(str(cfg.get('target_user_id') or ''),
                _selector_uuid(cfg.get('bot_uuid')), active_appointments(self._state.get('reminders', []), self.now()),
                bool(cfg.get('enabled', False)), cfg.get('study_bridge_db_path') or None)
        except Exception:
            LOGGER.exception('Study snapshot publication failed')

    async def remember_delivery(self, sender_id: Any, bot_uuid: str) -> None:
        if not self.matches(sender_id) or not bot_uuid:
            return
        async with self._lock:
            self._state["bot_uuid"] = bot_uuid
            self._state["target_id"] = str(sender_id)
            await self._save_locked()

    async def capture_prompt(self, sender_id: Any, default_prompt: Any, prompt: Any) -> None:
        if not self.matches(sender_id):
            return
        system = []
        for item in list(default_prompt or [])[-12:]:
            text = _content_text(getattr(item, "content", None)).strip()
            if text:
                system.append({"role": "system", "content": text[:6000]})
        history = []
        for item in list(prompt or [])[-10:]:
            role = str(getattr(item, "role", "") or "")
            text = _content_text(getattr(item, "content", None)).strip()
            if role in {"user", "assistant"} and text:
                history.append({"role": role, "content": text[:3000]})
        async with self._lock:
            self._state["prompt_context"] = {"system": system, "history": history, "captured_at": self.now().isoformat()}
            await self._save_locked()

    async def _invoke(self, model_uuid: str, messages: list[Message], max_tokens: int = 500) -> str:
        result = await self.plugin_runtime_handler.call_action(
            PluginToRuntimeAction.INVOKE_LLM,
            {
                "llm_model_uuid": model_uuid,
                "messages": [message.model_dump() for message in messages],
                "funcs": [],
                "extra_args": {"max_tokens": max_tokens, "thinking": {"type": "disabled"}},
                "_appointment_reminder_timeout": 85,
            },
            timeout=90.0,
        )
        return _strip_thinking(_response_text(Message.model_validate(result["message"])))

    def is_candidate(self, text: str) -> bool:
        return bool(is_reference(text) or ((TIME_PATTERN.search(text or "") or re.search(r'(明天|明晚|明日|后天).{0,10}[一二三四五六七八九十两]+[点時]', text or "")) and
                    (COMMITMENT_PATTERN.search(text or "") or STUDY_PATTERN.search(text or ""))))

    async def resolve_incoming_reference(self, sender_id, text):
        if not self.matches(sender_id):
            return False
        self._reference_questions.discard(str(sender_id))
        if not is_reference(text):
            return False
        cfg = self.cfg()
        if not cfg.get('enabled') or not cfg.get('natural_detection_enabled', True):
            return False
        context = self._state.get('prompt_context', {})
        result = resolve(text, context.get('history'), context.get('captured_at'), self.now())
        key = str(sender_id)
        self._pending_candidates.pop(key, None)
        if result['status'] != 'resolved':
            self._reference_questions.add(key)
            LOGGER.info('Referenced appointment requires a precise future time: %s', result['status'])
            return False
        self._reference_questions.discard(key)
        due = result['due_at']
        summary = f"{due.strftime('%H:%M')}按用户请求发消息联系。前一条原文：{result['reference']}"
        created, identifier = await self.add_reminder(due, summary, 'natural_reference', study_plan=False)
        if created:
            async with self._lock:
                item = next(item for item in self._state['reminders'] if item['id'] == identifier)
                item.update(evidence=text, reference=result['reference'])
                await self._save_locked()
        LOGGER.info('Referenced appointment stored: created=%s due_at=%s', created, due.isoformat())
        return True

    def needs_reference_question(self, sender_id):
        return str(sender_id) in self._reference_questions

    async def clear_conversation_reference(self, sender_id):
        key = str(sender_id)
        self._pending_candidates.pop(key, None)
        self._reference_questions.discard(key)
        self._last_user_turn.pop(key, None)
        async with self._lock:
            self._state.get('prompt_context', {})['history'] = []
            await self._save_locked()

    def remember_user_turn(self, sender_id, text):
        if self.matches(sender_id) and not text.startswith(('!', '/')):
            self._last_user_turn[str(sender_id)] = text

    async def remember_character_reply(self, sender_id, text):
        """Prompt preprocessing does not yet include the newly generated reply."""
        if not self.matches(sender_id) or not text:
            return
        pending = self._last_user_turn.pop(str(sender_id), '')
        async with self._lock:
            context = self._state.setdefault('prompt_context', {})
            history = context.setdefault('history', [])
            if pending and (not history or history[-1] != {'role': 'user', 'content': pending}):
                history.append({'role': 'user', 'content': pending})
            if not history or history[-1] != {'role': 'assistant', 'content': text}:
                history.append({'role': 'assistant', 'content': text[:3000]})
            context['history'] = history[-10:]
            context['captured_at'] = self.now().isoformat()
            await self._save_locked()

    def queue_inspection(self, sender_id, history, pending):
        async def run():
            try:
                await self.inspect_completed_conversation(sender_id, history, pending)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception('Background appointment inspection failed')
        job = asyncio.create_task(run(), name='appointment-extraction')
        self._inspection_tasks.add(job)
        job.add_done_callback(self._inspection_tasks.discard)

    def remember_candidate(self, sender_id: Any, text: str) -> None:
        """Retain the triggering user turn until NormalMessageResponded arrives.

        Some LangBot provider sessions expose no completed conversation history on
        that event, so relying on session.messages alone can silently lose an
        otherwise explicit reminder request.
        """
        if self.matches(sender_id):
            self._pending_candidates.pop(str(sender_id), None)
        if self.matches(sender_id) and self.is_candidate(text):
            self._pending_candidates[str(sender_id)] = text.strip()[:4000]

    def take_candidate(self, sender_id: Any) -> str:
        return self._pending_candidates.pop(str(sender_id), "")

    async def inspect_completed_conversation(
        self,
        sender_id: Any,
        messages: list[dict[str, str]],
        candidate_text: str | None = None,
    ) -> None:
        cfg = self.cfg()
        if not bool(cfg.get("enabled", False)) or not bool(cfg.get("natural_detection_enabled", True)):
            return
        if not self.matches(sender_id) or not messages:
            return
        current_user_text = str(candidate_text or "").strip()
        if not current_user_text:
            current_user_text = next(
                (
                    str(item.get("content") or "").strip()
                    for item in reversed(messages)
                    if item.get("role") == "user"
                ),
                "",
            )
        if not self.is_candidate(current_user_text):
            LOGGER.info("Appointment extraction skipped: no explicit time-and-commitment candidate")
            return
        model_uuid = _selector_uuid(cfg.get("model_uuid"))
        if not model_uuid:
            return
        now = self.now()
        transcript = "\n".join(f"{item['role']}: {item['content']}" for item in messages[-6:])[:8000]
        instruction = (
            "你是约定提取器，只判断标记为‘本轮用户消息’的文本是否刚刚形成了明确约定。"
            "历史对话只用于消歧，绝对不能从历史对话重新提取旧约定。必须同时满足："
            "(1) 用户明确要求提醒，或双方明确说好届时做某事，或用户明确决定在某个时间开展学习任务；(2) 时间能唯一换算为精确到分钟的未来时间；"
            "(3) 不是假设、玩笑、引用、已经完成的事或仅仅提到未来。模糊的‘改天/有空/下午’一律拒绝。"
            "只输出 JSON：{\"accepted\":true或false,\"due_at\":\"ISO8601含时区\","
            "\"summary\":\"简短约定事项\",\"evidence\":\"形成约定的短句\"}。不要输出解释。"
        )
        instruction += (
            '额外输出study_plan布尔值与evidence。仅用户自己明确预定的学习/复习/练习/看书/读书/自习等任务为true，'
            '课程自动提醒、娱乐、饮食、普通社交约定为false。evidence须逐字引用本轮用户消息。'
            '涉及学习但只是假设或询问，不是用户决定的安排，accepted必须false。'
            '例如“明天下午6点去图书馆看书，8点半回”是明确学习安排，起点18:00、结束20:30；'
            '无需额外说提醒或使用命令，登记18:00的学习约定，在summary保留地点和结束时间。'
        )
        try:
            raw = await self._invoke(
                model_uuid,
                [
                    Message(role="system", content=instruction),
                    Message(
                        role="user",
                        content=(
                            f"当前时间：{now.isoformat()}\n时区：{cfg.get('timezone','Asia/Shanghai')}"
                            f"\n本轮用户消息：\n{current_user_text}\n辅助历史：\n{transcript}"
                        ),
                    ),
                ],
                350,
            )
            parsed = _extract_json(raw)
        except Exception:
            LOGGER.exception("Appointment extraction failed")
            return
        if not parsed or parsed.get("accepted") is not True:
            LOGGER.info("Appointment extraction rejected by model")
            return
        due = _dt(parsed.get("due_at"))
        summary = re.sub(r"\s+", " ", str(parsed.get("summary") or "")).strip()[:300]
        if not due or not summary:
            LOGGER.info("Appointment extraction rejected: incomplete model result")
            return
        due = due.astimezone(self.tz())
        if due <= now + timedelta(seconds=30) or due > now + timedelta(days=366):
            LOGGER.info("Appointment extraction rejected: due time outside accepted future range")
            return
        study_plan = parsed.get('study_plan') is True and str(parsed.get('evidence') or '').strip() in current_user_text and bool(str(parsed.get('evidence') or '').strip())
        created, reminder_id = await self.add_reminder(due, summary, "natural", study_plan=study_plan)
        LOGGER.info("Appointment extraction stored: id=%s created=%s due_at=%s", reminder_id, created, due.isoformat())

    async def add_reminder(self, due: datetime, summary: str, source: str, study_plan: bool = False) -> tuple[bool, str]:
        cfg = self.cfg()
        fingerprint = hashlib.sha256(f"{due.isoformat()}|{summary.casefold()}".encode("utf-8")).hexdigest()[:16]
        async with self._lock:
            reminders = self._state.setdefault("reminders", [])
            # Different model runs may paraphrase the same appointment. The due
            # instant is the stable identity; do not schedule several messages
            # for the same target in the same minute.
            for item in reminders:
                existing_due = _dt(item.get("due_at"))
                if item.get("status") == "pending" and existing_due:
                    delta = abs((existing_due.astimezone(self.tz()) - due.astimezone(self.tz())).total_seconds())
                    if delta < 60:
                        if bool(item.get('study_plan')) != study_plan:
                            continue
                        if study_plan:
                            item['study_plan'] = True
                            await self._save_locked()
                        return False, str(item["id"])
            if any(item.get("fingerprint") == fingerprint and item.get("status") == "pending" for item in reminders):
                existing = next(item for item in reminders if item.get("fingerprint") == fingerprint and item.get("status") == "pending")
                return False, str(existing["id"])
            pending = [item for item in reminders if item.get("status") == "pending"]
            maximum = _clamp(cfg.get("max_pending"), 50, 1, 200)
            if len(pending) >= maximum:
                return False, "limit"
            reminder_id = uuid.uuid4().hex[:8]
            reminders.append({
                "id": reminder_id,
                "fingerprint": fingerprint,
                "due_at": due.astimezone(self.tz()).isoformat(),
                "summary": summary[:300],
                "source": source,
                "study_plan": study_plan,
                "status": "pending",
                "attempts": 0,
                "created_at": self.now().isoformat(),
            })
            await self._save_locked()
            return True, reminder_id

    async def list_pending(self) -> list[dict[str, Any]]:
        async with self._lock:
            if expire_studies(self._state.get("reminders", []), self.now()):
                await self._save_locked()
            items = [dict(item) for item in self._state.get("reminders", []) if item.get("status") == "pending"]
        return sorted(items, key=lambda item: str(item.get("due_at") or ""))

    async def cancel(self, reminder_id: str) -> bool:
        async with self._lock:
            for item in self._state.get("reminders", []):
                if item.get("status") == "pending" and str(item.get("id", "")).startswith(reminder_id):
                    item["status"] = "cancelled"
                    item["cancelled_at"] = self.now().isoformat()
                    await self._save_locked()
                    return True
        return False

    def parse_manual(self, text: str) -> tuple[datetime, str] | None:
        command = re.escape(str(self.cfg().get("add_command") or "!约定").strip())
        match = re.match(rf"^{command}\s+(\d{{4}}-\d{{2}}-\d{{2}})\s+(\d{{2}}:\d{{2}})\s+(.+)$", text.strip(), flags=re.S | re.I)
        if not match:
            return None
        try:
            due = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M").replace(tzinfo=self.tz())
        except ValueError:
            return None
        summary = re.sub(r"\s+", " ", match.group(3)).strip()
        return (due, summary[:300]) if due > self.now() and summary else None

    async def _render_due(self, reminder: dict[str, Any], now: datetime) -> str:
        cfg = self.cfg()
        due = _dt(reminder.get("due_at")) or now
        template = str(cfg.get("fixed_message_template") or "到约定的时间了：{summary}")
        fallback = template.replace("{summary}", str(reminder.get("summary") or "")).replace("{time}", due.strftime("%Y-%m-%d %H:%M"))
        if not bool(cfg.get("persona_message_enabled", True)):
            return fallback[:2000]
        model_uuid = _selector_uuid(cfg.get("model_uuid"))
        if not model_uuid:
            return fallback[:2000]
        async with self._lock:
            context = dict(self._state.get("prompt_context") or {})
        messages: list[Message] = []
        for item in context.get("system", [])[-10:]:
            if item.get("content"):
                messages.append(Message(role="system", content=str(item["content"])))
        messages.append(Message(role="system", content=(
            "现在是此前明确约定的到点发言。保持现有人设与称呼，自然提醒对方；"
            "不得修改约定事项，不得声称对方已经做了什么，不提及插件、计划任务或模型。"
            "只输出一条通过手机软件发送的消息，以半角*开头，随后仅写要发送的话；不写神态、动作、旁白或包裹整句的引号。"
            "若当前时间已经晚于约定时间一分钟以上，要自然说明联系迟了；不要把过去的约定说成刚刚到点，也不要改成明天。"
        )))
        for item in context.get("history", [])[-6:]:
            if item.get("role") in {"user", "assistant"} and item.get("content"):
                messages.append(Message(role=item["role"], content=str(item["content"])))
        messages.append(Message(role="user", content=f"约定时间：{due.strftime('%Y-%m-%d %H:%M')}\n约定事项：{reminder.get('summary')}\n当前时间：{now.strftime('%Y-%m-%d %H:%M')}"))
        try:
            generated = await self._invoke(model_uuid, messages, 400)
            return generated[:2000] if generated else fallback[:2000]
        except Exception:
            LOGGER.exception("Due-message generation failed; using fixed template")
            return fallback[:2000]

    async def _tick(self) -> None:
        async with self._lock:
            if expire_studies(self._state.get("reminders", []), self.now()):
                await self._save_locked()
        cfg = self.cfg()
        if not bool(cfg.get("enabled", False)):
            self._export_study()
            return
        now = self.now()
        grace = timedelta(minutes=_clamp(cfg.get("late_grace_minutes"), 360, 0, 10080))
        max_attempts = _clamp(cfg.get("send_max_attempts"), 3, 1, 10)
        async with self._send_lock:
            async with self._lock:
                reminders = self._state.get("reminders", [])
                chosen = None
                for item in reminders:
                    due = _dt(item.get("due_at"))
                    if item.get("status") != "pending" or not due or due > now:
                        continue
                    if now - due > grace:
                        item["status"] = "expired"
                        continue
                    next_attempt = _dt(item.get("next_attempt_at"))
                    if next_attempt and next_attempt > now:
                        continue
                    chosen = dict(item)
                    break
                await self._save_locked()
            if chosen is None:
                return
            publish(str(cfg.get('target_user_id') or ''), 'appointment.due', resource='appointment', busy=True)
            text = await self._render_due(chosen, now)
            text = '*' + text.lstrip().lstrip('*').lstrip()
            bot_uuid = str(self._state.get("bot_uuid") or "") or _selector_uuid(cfg.get("bot_uuid"))
            target_id = str(cfg.get("target_user_id") or "").strip()
            async with self._lock:
                fresh = self.cfg()
                same_destination = (
                    bool(fresh.get("enabled", False))
                    and _selector_uuid(fresh.get("bot_uuid")) == _selector_uuid(cfg.get("bot_uuid"))
                    and str(fresh.get("target_user_id") or "").strip() == target_id
                )
                if expired_study(chosen, self.now()):
                    await self._save_locked()
                    publish(target_id, 'appointment.deferred', resource='appointment', busy=False)
                    return
                still_pending = any(
                    item.get("id") == chosen.get("id") and item.get("status") == "pending"
                    for item in self._state.get("reminders", [])
                )
            if not same_destination or not still_pending:
                publish(target_id, 'appointment.deferred', resource='appointment', busy=False)
                return
            try:
                await self.send_message(bot_uuid, "person", target_id, MessageChain([Plain(text=text)]))
            except Exception:
                publish(target_id, 'appointment.failed', resource='appointment', busy=False)
                LOGGER.exception("Appointment reminder send failed")
                async with self._lock:
                    for item in self._state.get("reminders", []):
                        if item.get("id") == chosen.get("id"):
                            item["attempts"] = int(item.get("attempts", 0)) + 1
                            if item["attempts"] >= max_attempts:
                                item["status"] = "failed"
                            else:
                                item["next_attempt_at"] = (now + timedelta(minutes=2)).isoformat()
                    await self._save_locked()
                return
            publish(target_id, 'appointment.sent', {'text': text}, resource='appointment', busy=False)
            async with self._lock:
                for item in self._state.get("reminders", []):
                    if item.get("id") == chosen.get("id"):
                        item["status"] = "delivered"
                        item["delivered_at"] = self.now().isoformat()
                await self._save_locked()

    async def _scheduler(self) -> None:
        try:
            await asyncio.sleep(10)
            while True:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("Appointment scheduler tick failed")
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            return
