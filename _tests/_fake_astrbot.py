"""一个「最小可用的 AstrBot 桩」，用于在没装 AstrBot 的环境里跑本插件的测试。

为什么需要它
============
放风功能的时长解析（`_parse_duration` / `_classify_vent`）是纯函数，可以直接测；
但「谁能放风」「放风期间是否放行」「到期是否收回」这些要真的把插件 import 起来、
按 AstrBot 的调度语义把 handler 跑一遍才说得清。这里用最小桩把
``astrbot.api.*`` 补上：

1. import main.py（否则 `from astrbot.api import ...` 会直接 ImportError）；
2. 复刻 handler 的调用方式（`on_message` 协程 + `on_llm_request` 钩子）；
3. 复刻 `event.should_call_llm(True)` / `event.stop_event()` 的语义；
4. 复刻插件 KV（`get_kv_data` / `put_kv_data`）以验证放风状态持久化。

⚠️ 桩不是 AstrBot 本身，真实运行时请以 AstrBot 源码为准（对照版本：v4.9.2 ~
v4.29，只用到两个版本都有的 API）。
"""

from __future__ import annotations

import enum
import sys
import types

# 插件 KV 的替身存储：key = plugin_id，value = {kv_key: value}
_KV_STORE: dict[str, dict] = {}


def install() -> None:
    """把 astrbot 相关的模块塞进 sys.modules（重复调用无副作用）。"""
    if "astrbot" in sys.modules and getattr(
        sys.modules["astrbot"], "__is_llmallowlist_stub__", False
    ):
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.__is_llmallowlist_stub__ = True
    api = types.ModuleType("astrbot.api")
    api_event = types.ModuleType("astrbot.api.event")
    api_star = types.ModuleType("astrbot.api.star")
    msg_components = types.ModuleType("astrbot.api.message_components")
    api_platform = types.ModuleType("astrbot.api.platform")

    # ------------------------------------------------------------ logger
    class _Logger:
        def __init__(self):
            self.records: list[tuple[str, str]] = []

        def _log(self, level, msg, *args):
            try:
                text = msg % args if args else str(msg)
            except Exception:
                text = f"{msg} {args}"
            self.records.append((level, text))

        def debug(self, msg, *args):
            self._log("debug", msg, *args)

        def info(self, msg, *args):
            self._log("info", msg, *args)

        def warning(self, msg, *args):
            self._log("warning", msg, *args)

        def error(self, msg, *args):
            self._log("error", msg, *args)

    logger = _Logger()

    class AstrBotConfig(dict):
        """AstrBotConfig 的最小替身。"""

    # ------------------------------------------------------------ MessageChain
    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain) if chain else []

        def message(self, text):
            self.chain = [Plain(text)]
            return self

    class Plain:
        def __init__(self, text=""):
            self.text = text

        def __repr__(self):
            return f"Plain({self.text!r})"

    class At:
        def __init__(self, qq=""):
            self.qq = qq

    class Reply:
        def __init__(self, id=""):
            self.id = id

    # ------------------------------------------------------------ MessageType
    class MessageType(enum.Enum):
        GROUP_MESSAGE = "GroupMessage"
        FRIEND_MESSAGE = "FriendMessage"

    # ------------------------------------------------------------ filter
    class PlatformAdapterType:
        ALL = "all"
        AIOCQHTTP = "aiocqhttp"

    class EventMessageType:
        ALL = "all"
        GROUP_MESSAGE = "group"

    class PermissionType:
        ADMIN = "admin"

    class _Filter:
        # 真实 AstrBot 里 filter 是个模块，PlatformAdapterType / EventMessageType
        # 是它的模块属性，插件写的是 filter.PlatformAdapterType.ALL，这里照抄。

        @staticmethod
        def platform_adapter_type(*args, **kwargs):
            def deco(func):
                return func

            return deco

        @staticmethod
        def event_message_type(*args, **kwargs):
            def deco(func):
                return func

            return deco

        @staticmethod
        def on_llm_request(*args, **kwargs):
            def deco(func):
                return func

            return deco

        @staticmethod
        def command(*args, **kwargs):
            def deco(func):
                return func

            return deco

    _Filter.PlatformAdapterType = PlatformAdapterType
    _Filter.EventMessageType = EventMessageType
    _Filter.PermissionType = PermissionType

    # ------------------------------------------------------------ Star / Context
    global _KV_STORE
    _KV_STORE.clear()

    class Context:
        def __init__(self):
            self.sent: list[tuple[str, str]] = []

        async def send_message(self, session, message_chain) -> bool:
            text = "".join(getattr(c, "text", "") for c in getattr(message_chain, "chain", []))
            self.sent.append((str(session), text))
            return True

    class Star:
        def __init__(self, context, config=None):
            self.context = context
            self.plugin_id = "astrbot_plugin_llmallowlist"

        async def get_kv_data(self, key, default=None):
            return _KV_STORE.get(self.plugin_id, {}).get(key, default)

        async def put_kv_data(self, key, value):
            _KV_STORE.setdefault(self.plugin_id, {})[key] = value

    # 暴露给测试文件用（install() 里定义的类默认是局部名）
    globals().update(
        Context=Context,
        MessageChain=MessageChain,
        Plain=Plain,
        MessageType=MessageType,
        AstrBotConfig=AstrBotConfig,
        logger=logger,
    )

    # ------------------------------------------------------------ 组装模块
    api.AstrBotConfig = AstrBotConfig
    api.logger = logger
    api_event.filter = _Filter
    api_event.AstrMessageEvent = object
    api_event.MessageChain = MessageChain
    api_star.Context = Context
    api_star.Star = Star
    api_star.StarTools = object
    msg_components.Plain = Plain
    msg_components.At = At
    msg_components.Reply = Reply
    api_platform.MessageType = MessageType

    astrbot.api = api
    astrbot.logger = logger
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": api_event,
            "astrbot.api.star": api_star,
            "astrbot.api.message_components": msg_components,
            "astrbot.api.platform": api_platform,
        }
    )


def reset_kv() -> None:
    """清空桩里的插件 KV（测试之间互不干扰）。"""
    _KV_STORE.clear()


# ================================================================ 假 event
class _MessageObj:
    def __init__(self, group_id="", message_id="m1"):
        self.group_id = group_id
        self.message_id = message_id


class FakeBot:
    """提供 `bot.api.call_action("get_group_member_list")`。"""

    def __init__(self, admins=None):
        self.admins = set(str(a) for a in (admins or []))
        self.calls = 0

        class _Api:
            def __init__(self, outer):
                self.outer = outer

            async def call_action(self, action, **kwargs):
                self.outer.calls += 1
                assert action == "get_group_member_list"
                return [
                    {"user_id": uid, "role": "admin"} for uid in sorted(self.outer.admins)
                ]

        self.api = _Api(self)


class FakeEvent:
    """够用的 AstrMessageEvent 替身。"""

    def __init__(
        self,
        text="",
        sender_id="10001",
        group_id="100",
        platform="aiocqhttp",
        is_admin=False,
        bot=None,
        message_type=None,
    ):
        self.message_str = text
        self._sender_id = str(sender_id)
        self._group_id = str(group_id)
        self._platform = platform
        self._is_admin = bool(is_admin)
        self.bot = bot
        self.message_obj = _MessageObj(group_id=str(group_id))
        self._message_type = message_type or MessageType.GROUP_MESSAGE
        self.unified_msg_origin = f"{platform}:GroupMessage:{group_id}"
        # 复刻 AstrBot 语义：call_llm=True 表示「禁止默认 LLM 链路」
        self.call_llm = False
        self._stopped = False
        self.sent: list[str] = []

    # ---- AstrMessageEvent 接口
    def get_message_str(self) -> str:
        return self.message_str

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_group_id(self) -> str:
        return self._group_id

    def get_platform_name(self) -> str:
        return self._platform

    def get_message_type(self):
        return self._message_type

    def get_self_id(self) -> str:
        return "99999"

    def is_admin(self) -> bool:
        return self._is_admin

    def should_call_llm(self, call_llm: bool) -> None:
        self.call_llm = call_llm

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped

    async def send(self, message_chain):
        text = "".join(getattr(c, "text", "") for c in getattr(message_chain, "chain", []))
        self.sent.append(text)


def make_config(**overrides):
    """按 _conf_schema.json 的默认值造一份配置，再叠加 overrides。"""
    cfg = {
        "enable_groups": "",
        "admin_bypass": True,
        "allowlist": "aiocqhttp[111]",
        "reply_msg": "aiocqhttp[不在白名单]",
        "vent_enabled": True,
        "vent_allowlist_can_vent": False,
        "vent_default_minutes": 5,
        "vent_max_minutes": 120,
        "vent_expire_notify": True,
    }
    cfg.update(overrides)
    return cfg
