import re
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Reply as CompReply, At as CompAt
from astrbot.api.star import Context, Star


class LLMAllowList(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._group_admins: dict[str, dict] = {}

    def _parse_platform_map(self, key):
        raw = self.config.get(key, "")
        if not raw or not raw.strip():
            return {}
        result = {}
        for line in raw.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r'^(\w+)\[([^\]]*)\]$', line)
            if m:
                platform = m.group(1)
                content = m.group(2).strip()
                result[platform] = content
        return result

    def _parse_allowlist(self):
        pmap = self._parse_platform_map("allowlist")
        return {p: {u.strip() for u in v.split(",") if u.strip()} for p, v in pmap.items()}

    async def _fetch_group_admins(self, bot, group_id: str):
        try:
            data = await bot.api.call_action("get_group_member_list", group_id=group_id)
            if data:
                admins = {str(m["user_id"]) for m in data if m.get("role") in ("owner", "admin")}
                self._group_admins[group_id] = {"admins": admins, "fetched_at": time.time()}
                logger.debug("[llm_allowlist] admin_fetch | 群%s 管理员%d人: %s", group_id, len(admins), admins)
        except Exception as e:
            logger.debug("[llm_allowlist] admin_fetch | 群%s 获取失败: %s", group_id, e)

    async def _send_reply(self, event: AstrMessageEvent, text: str):
        try:
            mc = MessageChain()
            mc.chain = [CompReply(id=event.message_obj.message_id), Plain(text)]
            await event.send(mc)
        except Exception:
            try:
                mc = MessageChain()
                mc.chain = [CompAt(qq=event.get_sender_id()), Plain(text)]
                await event.send(mc)
            except Exception:
                mc = MessageChain().message(text)
                await event.send(mc)

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        sender_id = event.get_sender_id()
        platform = event.get_platform_name()

        if self.config.get("admin_bypass", False):
            gid = str(event.message_obj.group_id or "")
            evt_bot = getattr(event, "bot", None)
            if gid and evt_bot:
                cache = self._group_admins.get(gid)
                if not cache or time.time() - cache["fetched_at"] > 3600:
                    await self._fetch_group_admins(evt_bot, gid)
                cache = self._group_admins.get(gid, {})
                admins = cache.get("admins", set())
                logger.debug(
                    "[llm_allowlist] admin_check | platform=%s sender_id=%s "
                    "in_admins=%s admins=%s",
                    platform, sender_id, sender_id in admins, admins,
                )
                if sender_id in admins:
                    return

        allowlist = self._parse_allowlist()

        if sender_id in allowlist.get(platform, set()):
            return

        reply_msgs = self._parse_platform_map("reply_msg")
        reply = reply_msgs.get(platform)
        if reply:
            await self._send_reply(event, reply)

        event.should_call_llm(True)
