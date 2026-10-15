"""放风（临时全群放行）的纯逻辑 + 窗口表。

拆出来是为了让 main.py 只留插件胶水（配置解析 / 权限判定 / handler / hook），
同时本模块不 import astrbot，可以直接单测（见 _tests/test_vent.py）。

- 纯函数：``classify_vent`` 判定「这条消息是不是放风指令」，
  ``parse_duration`` 解析「5分钟 / 半小时 / 1小时30分」这类时长。
- ``VentWindows``：每个群一个放风窗口（到期时间 + 会话标识），负责 KV 持久化与
  到期任务；到期时通过 ``on_expire`` 回调让插件去发提醒。
"""

import asyncio
import re
import time

# 只有「以这些词开头」并且「后面的内容能解析成时长（或本身是状态/收风指令）」
# 的消息才会被当成放风指令；其余一律原样交给后续流程，避免误伤「放风筝」这类聊天。
VENT_CMD_RE = re.compile(
    r"^\s*[/!#]?\s*(?P<word>"
    r"放风状态|放风剩余|放风查询|查看放风|查放风|"
    r"关放风|停放风|收放风|取消放风|结束放风|"
    r"放风|收风"
    r")\s*(?P<arg>.*)$"
)
# 本身就表示「查状态」的词
VENT_STATUS_WORDS = {"放风状态", "放风剩余", "放风查询", "查看放风", "查放风"}
# 本身就表示「结束放风」的词
VENT_CLOSE_WORDS = {"关放风", "停放风", "收放风", "取消放风", "结束放风", "收风"}
# 「放风 <这些词>」等价于查状态
VENT_STATUS_ARGS = {"状态", "剩余", "查询", "多久", "还有多久", "状态查询", "?", "？"}
# 允许出现在指令尾部的语气词 / 标点（「收风吧」「放风状态？」）
VENT_TAIL_CHARS = " \t吧呢啊呀哦嘛了吗滴~！!。.,，、;；?？"

# 时长 token：数字（阿拉伯或中文）+ 单位，例如 `5分钟` / `30s` / `1小时30分` / `半小时`
_VENT_TOKEN_RE = re.compile(
    r"(\d+(?:\.\d+)?|[零一二两三四五六七八九十半]+)\s*([a-zA-Z\u4e00-\u9fff]*)"
)
# 单位 -> 秒；未识别的单位按「分钟」处理（「5分钟吧」也能吃下），无单位默认分钟
_UNIT_SECONDS = {
    "": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "秒": 1, "秒钟": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "分": 60, "分钟": 60,
    "刻": 900, "刻钟": 900,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "时": 3600, "小时": 3600, "个小时": 3600,
    "d": 86400, "day": 86400, "days": 86400, "天": 86400, "日": 86400,
}
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def cn_number(text: str) -> float | None:
    """中文数字转数值，支持「半」「一~九」「十」「十五」「二十」「二十五」。

    Args:
        text: 纯中文数字片段。

    Returns:
        数值；无法识别返回 None。
    """
    if not text:
        return None
    if text in ("半", "半个"):
        return 0.5
    if "十" in text:
        head, _, tail = text.partition("十")
        if head and head not in _CN_DIGITS:
            return None
        if tail and tail not in _CN_DIGITS:
            return None
        tens = _CN_DIGITS[head] if head else 1
        ones = _CN_DIGITS[tail] if tail else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(text)


def num_value(raw: str) -> float | None:
    """把 token 里的数字部分转成 float（阿拉伯数字或中文数字）。"""
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return float(raw)
    return cn_number(raw)


def parse_duration(text: str) -> int:
    """把「5分钟 / 30s / 1小时30分 / 半小时 / 十分钟」解析成秒数。

    Args:
        text: 指令后面的时长文本。

    Returns:
        总秒数；一个数字都没解析出来时返回 0。
    """
    total = 0.0
    for raw_num, unit in _VENT_TOKEN_RE.findall(text or ""):
        value = num_value(raw_num)
        if value is None:
            continue
        total += value * _UNIT_SECONDS.get(unit, 60)
    return int(total)


def fmt_duration(seconds: float) -> str:
    """把秒数格式化成人看的时长，例如 `30秒` / `5分钟` / `1小时30分钟`。"""
    total = int(round(seconds))
    hour, rem = divmod(total, 3600)
    minute, second = divmod(rem, 60)
    parts = []
    if hour:
        parts.append(f"{hour}小时")
    if minute:
        parts.append(f"{minute}分钟")
    if second and not hour:
        parts.append(f"{second}秒")
    return "".join(parts) or "0秒"


def is_bare_tail(arg: str) -> bool:
    """参数是否只剩语气词 / 标点（即等于没写参数）。"""
    return not arg.strip(VENT_TAIL_CHARS)


def classify_vent(text: str) -> tuple[str, str] | None:
    """判断一条消息是不是放风指令。

    Args:
        text: 消息纯文本（唤醒前缀已被框架剥掉，这里再兼容一次 `/`、`!`、`#`）。

    Returns:
        ``(action, arg)``，action 为 ``open`` / ``close`` / ``status``；
        不像放风指令时返回 None（交给原有白名单流程处理）。
    """
    m = VENT_CMD_RE.match((text or "").strip())
    if not m:
        return None
    word = m.group("word")
    arg = (m.group("arg") or "").strip()
    if word in VENT_STATUS_WORDS:
        return ("status", "") if is_bare_tail(arg) else None
    if word in VENT_CLOSE_WORDS:
        return ("close", "") if is_bare_tail(arg) else None
    # 只剩「放风」本身
    if arg in VENT_STATUS_ARGS:
        return ("status", "")
    if not arg:
        return ("open", "")
    return ("open", arg) if parse_duration(arg) > 0 else None


class VentWindows:
    """放风窗口表。

    每个窗口：``key = "平台:群号"`` -> ``{"expire": 到期时间戳, "umo": 会话标识}``。
    只影响发指令的那个群；窗口写进插件 KV，插件重载 / 机器人重启后未到期的窗口仍有效。

    Args:
        kv_get: ``async (key, default) -> value``，读插件 KV。
        kv_put: ``async (key, value)``，写插件 KV。
        on_expire: ``async (key, umo)``，窗口到期时的回调（由插件决定是否提醒）。
        logger: 可选日志器，只用 debug/info。
    """

    KV_KEY = "vent_windows"

    def __init__(self, *, kv_get, kv_put, on_expire, logger=None):
        self._kv_get = kv_get
        self._kv_put = kv_put
        self._on_expire = on_expire
        self.log = logger
        self._windows: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._loaded = False

    @staticmethod
    def make_key(platform: str, group_id) -> str:
        """窗口键：平台 + 群号。"""
        return f"{platform}:{group_id or ''}"

    def left(self, key: str) -> float:
        """该窗口剩余秒数；顺手清掉已过期的窗口。"""
        item = self._windows.get(key)
        if not item:
            return 0.0
        left = float(item.get("expire", 0) or 0) - time.time()
        if left <= 0:
            self._windows.pop(key, None)
            return 0.0
        return left

    def is_open(self, key: str) -> bool:
        """该群是否正在放风。"""
        return self.left(key) > 0

    def _log(self, level: str, msg: str, *args):
        if self.log:
            getattr(self.log, level, self.log.debug)(msg, *args)

    async def ensure_loaded(self):
        """首次用到放风状态时，从插件 KV 恢复未过期的窗口并重排到期任务。"""
        if self._loaded:
            return
        self._loaded = True
        try:
            data = await self._kv_get(self.KV_KEY, {})
        except Exception as e:
            self._log("debug", "[llm_allowlist] vent_load | 读取持久化放风状态失败: %s", e)
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        for key, item in data.items():
            if not isinstance(item, dict):
                continue
            left = float(item.get("expire", 0) or 0) - now
            if left <= 0:
                continue
            self._windows[key] = {"expire": now + left, "umo": str(item.get("umo") or "")}
        if not self._windows:
            return
        for key, item in list(self._windows.items()):
            self._schedule(key, item["umo"], int(item["expire"] - time.time()) + 1)
        self._log("info", "[llm_allowlist] vent_load | 恢复 %d 个未到期放风窗口", len(self._windows))

    async def save(self):
        """把窗口表写回插件 KV（失败不影响功能，只是重启后不保留）。"""
        try:
            await self._kv_put(self.KV_KEY, self._windows)
        except Exception as e:
            self._log("debug", "[llm_allowlist] vent_save | 持久化放风状态失败: %s", e)

    async def open(self, key: str, umo: str, seconds: int):
        """开启 / 续期某群的放风窗口。"""
        self._windows[key] = {"expire": time.time() + seconds, "umo": umo}
        self._schedule(key, umo, seconds)
        await self.save()

    async def close(self, key: str):
        """立即收回某群的放风窗口。"""
        self._windows.pop(key, None)
        task = self._tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
        await self.save()

    async def shutdown(self):
        """插件被禁用 / 重载时取消全部到期任务（窗口状态已即时落盘）。"""
        pending, self._tasks = list(self._tasks.values()), {}
        for task in pending:
            if not task.done():
                task.cancel()

    def _schedule(self, key: str, umo: str, seconds: int):
        """给某个窗口挂到期任务（到期自动收回 + 回调提醒）。"""
        old = self._tasks.pop(key, None)
        if old and not old.done():
            old.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._log("debug", "[llm_allowlist] vent | 当前无事件循环，跳过到期提醒")
            return
        self._tasks[key] = loop.create_task(self._expire(key, umo, seconds))

    async def _expire(self, key: str, umo: str, seconds: int):
        """到期：收回窗口，然后交给插件决定要不要吭一声。"""
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        item = self._windows.get(key)
        if not item or float(item.get("expire", 0) or 0) > time.time():
            return  # 已被延长或被手动收风
        self._windows.pop(key, None)
        self._tasks.pop(key, None)
        await self.save()
        self._log("info", "[llm_allowlist] vent_expire | %s 放风结束", key)
        if self._on_expire:
            await self._on_expire(key, umo)
