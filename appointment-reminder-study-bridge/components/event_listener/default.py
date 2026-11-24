from __future__ import annotations

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events
from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain
from langbot_plugin.api.entities.builtin.provider.message import Message
from main import _selector_uuid


def chain_text(chain) -> str:
    return "\n".join(component.text for component in chain or [] if isinstance(component, Plain) and component.text).strip()


def serialize_history(messages) -> list[dict[str, str]]:
    result = []
    for item in list(messages or [])[-8:]:
        raw_role = getattr(item, "role", "") or ""
        role = str(getattr(raw_role, "value", raw_role)).strip().lower()
        content = getattr(item, "content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(str(getattr(part, "text", "") or (part.get("text", "") if isinstance(part, dict) else "")) for part in content)
        else:
            text = ""
        if role in {"user", "assistant"} and text.strip():
            result.append({"role": role, "content": text.strip()[:3000]})
    return result


class AppointmentReminderListener(EventListener):
    async def initialize(self) -> None:
        await super().initialize()

        @self.handler(events.PersonMessageReceived)
        async def on_private(event_context: context.EventContext) -> None:
            event = event_context.event
            if not self.plugin.matches(event.sender_id):
                return
            text = chain_text(event.message_chain)
            bot_uuid = str(await event_context.get_bot_uuid())
            expected_bot = _selector_uuid(self.plugin.cfg().get('bot_uuid'))
            if expected_bot and bot_uuid != expected_bot:
                return
            try:
                await self.plugin.remember_delivery(event.sender_id, bot_uuid)
            except Exception:
                pass
            cfg = self.plugin.cfg()
            if text in ('!reset', '/reset'):
                await self.plugin.clear_conversation_reference(event.sender_id)
                return
            list_cmd = str(cfg.get("list_command") or "!约定列表").strip()
            cancel_cmd = str(cfg.get("cancel_command") or "!取消约定").strip()
            add_cmd = str(cfg.get("add_command") or "!约定").strip()
            if text.casefold() == list_cmd.casefold():
                event_context.prevent_default()
                event_context.prevent_postorder()
                items = await self.plugin.list_pending()
                if not items:
                    reply = "目前没有待触发的约定。"
                else:
                    lines = ["待触发约定："]
                    for item in items:
                        due = item.get("due_at", "").replace("T", " ")[:16]
                        lines.append(f"{item.get('id')}｜{due}｜{item.get('summary')}")
                    reply = "\n".join(lines)
                await event_context.reply(MessageChain([Plain(text=reply)]))
                return
            if text.casefold().startswith(cancel_cmd.casefold() + " "):
                event_context.prevent_default()
                event_context.prevent_postorder()
                reminder_id = text[len(cancel_cmd):].strip()
                ok = await self.plugin.cancel(reminder_id)
                await event_context.reply(MessageChain([Plain(text="已取消这条约定。" if ok else "没有找到对应的待触发约定。")]))
                return
            if text.casefold().startswith(add_cmd.casefold() + " "):
                event_context.prevent_default()
                event_context.prevent_postorder()
                parsed = self.plugin.parse_manual(text)
                if not parsed:
                    await event_context.reply(MessageChain([Plain(text=f"用法：{add_cmd} 2026-09-05 15:00 提醒内容")]))
                    return
                created, reminder_id = await self.plugin.add_reminder(parsed[0], parsed[1], "command")
                if reminder_id == "limit":
                    reply = "待触发约定已达到上限，请先取消或等待已有约定完成。"
                elif created:
                    reply = f"约定已记下（{reminder_id}）：{parsed[0]:%Y-%m-%d %H:%M}｜{parsed[1]}"
                else:
                    reply = f"这条约定已经存在（{reminder_id}）。"
                await event_context.reply(MessageChain([Plain(text=reply)]))
                return
            self.plugin.remember_candidate(event.sender_id, text)
            self.plugin.remember_user_turn(event.sender_id, text)
            await self.plugin.resolve_incoming_reference(event.sender_id, text)

        @self.handler(events.PromptPreProcessing)
        async def on_prompt(event_context: context.EventContext) -> None:
            event = event_context.event
            try:
                sender_id = (await event_context.get_query_vars()).get("sender_id")
            except Exception:
                sender_id = None
            if sender_id is None:
                name = str(getattr(event, "session_name", "") or "")
                sender_id = name.removeprefix("person_") if name.startswith("person_") else None
            if sender_id is not None:
                expected_bot = _selector_uuid(self.plugin.cfg().get('bot_uuid'))
                if expected_bot and str(await event_context.get_bot_uuid()) != expected_bot:
                    return
                await self.plugin.capture_prompt(sender_id, event.default_prompt, event.prompt)
                if self.plugin.needs_reference_question(sender_id):
                    event.prompt.append(Message(role='system', content='本轮用户要求“那个时间/到时候”提醒，但目前无法唯一确定未来时刻。请按现有人设和语言问清具体时间；确认前不要承诺已经安排好定时联系。'))
                    self.plugin._reference_questions.discard(str(sender_id))

        @self.handler(events.NormalMessageResponded)
        async def on_responded(event_context: context.EventContext) -> None:
            event = event_context.event
            if (str(getattr(event, 'launcher_type', 'person')) != 'person'
                    or not self.plugin.matches(event.sender_id) or getattr(event, "funcs_called", [])):
                return
            conversation = getattr(event.session, "using_conversation", None)
            history = serialize_history(getattr(conversation, "messages", [])) if conversation else []
            response_text = str(getattr(event, "response_text", "") or "").strip()
            await self.plugin.remember_character_reply(event.sender_id, response_text)
            pending = self.plugin.take_candidate(event.sender_id)
            # Never rescan old conversation turns. Without a candidate captured
            # from this exact incoming user message there is no new appointment.
            if not pending:
                return
            if pending and not any(item.get("role") == "user" and item.get("content") == pending for item in history[-3:]):
                history.append({"role": "user", "content": pending})
            if response_text and (not history or history[-1].get("role") != "assistant"):
                history.append({"role": "assistant", "content": response_text[:3000]})
            # The runtime gives event callbacks only 15 seconds. Model extraction
            # must not delay or swallow the ordinary character response.
            self.plugin.queue_inspection(event.sender_id, history, pending)
