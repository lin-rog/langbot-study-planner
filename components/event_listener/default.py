from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import events
from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain


class StudyListener(EventListener):
    async def initialize(self):
        await super().initialize()

        @self.handler(events.PersonMessageReceived)
        async def receive(ctx):
            event = ctx.event
            bot = await ctx.get_bot_uuid()
            if not self.plugin.matches(event.sender_id, bot):
                return
            if not await self.plugin.in_pipeline(ctx):
                return
            await self.plugin.ensure_context_scope()
            text = '\n'.join(p.text for p in event.message_chain if isinstance(p, Plain)).strip()
            normal = not text.startswith(('!', '/'))
            if normal:
                await self.plugin.record_user_turn(text)
            response = await self.plugin.command(text)
            if response is None:
                response = await self.plugin.natural_control(text)
            if response is not None:
                ctx.prevent_default()
                ctx.prevent_postorder()
                await ctx.reply(MessageChain([Plain(text=response)]))
                if normal:
                    await self.plugin.record_reply('')

        @self.handler(events.PromptPreProcessing)
        async def capture(ctx):
            variables = await ctx.get_query_vars()
            sender = variables.get('sender_id')
            if sender is None:
                name = str(getattr(ctx.event, 'session_name', '') or '')
                sender = name.removeprefix('person_') if name.startswith('person_') else None
            if sender is not None:
                bot = await ctx.get_bot_uuid()
                if self.plugin.matches(sender, bot) and await self.plugin.in_pipeline(ctx):
                    await self.plugin.ensure_context_scope()
                    await self.plugin.capture_persona(sender, bot, ctx.event.default_prompt)
                    self.plugin.inject_dialogue(ctx.event.prompt)

        @self.handler(events.NormalMessageResponded)
        async def completed(ctx):
            event = ctx.event
            if (str(getattr(event, 'launcher_type', 'person')) == 'person'
                    and self.plugin.matches(event.sender_id, await ctx.get_bot_uuid())
                    and await self.plugin.in_pipeline(ctx)
                    and not getattr(event, 'funcs_called', [])):
                await self.plugin.record_reply(str(getattr(event, 'response_text', '') or '').strip())
