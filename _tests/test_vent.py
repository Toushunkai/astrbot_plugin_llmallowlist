"""放风功能测试。

跑法： python3 _tests/test_vent.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(HERE))
import _fake_astrbot as fake  # noqa: E402

fake.install()

# 按 AstrBot 的方式「以包的形式」加载插件（star_manager 用
# __import__("data.plugins.<目录>.main")），这样 main.py 里的
# `from .vent import ...` 能正常解析。
import types  # noqa: E402

_pkg = types.ModuleType("llmallowlist_pkg")
_pkg.__path__ = [str(ROOT)]
sys.modules["llmallowlist_pkg"] = _pkg
_spec = importlib.util.spec_from_file_location("llmallowlist_pkg.main", ROOT / "main.py")
plugin = importlib.util.module_from_spec(_spec)
sys.modules["llmallowlist_pkg.main"] = plugin
_spec.loader.exec_module(plugin)
vent = sys.modules["llmallowlist_pkg.vent"]


# =============================================================== 纯函数
def test_parse_duration():
    p = vent.parse_duration
    cases = {
        "5分钟": 300,
        "5 分": 300,
        "30s": 30,
        "30秒": 30,
        "1小时30分": 5400,
        "半小时": 1800,
        "十分钟": 600,
        "二十五分钟": 1500,
        "2小时": 7200,
        "90": 5400,
        "1.5小时": 5400,
        "一刻钟": 900,
        "1天": 86400,
        "没有数字": 0,
        "": 0,
    }
    for text, want in cases.items():
        got = p(text)
        assert got == want, f"parse_duration({text!r}) = {got}, 期望 {want}"
    print("  ✅ parse_duration")


def test_classify_vent():
    c = vent.classify_vent
    cases = {
        "放风5分钟": ("open", "5分钟"),
        "放风": ("open", ""),
        "放风 30分钟": ("open", "30分钟"),
        "/放风5分钟": ("open", "5分钟"),
        "放风十分钟": ("open", "十分钟"),
        "收风": ("close", ""),
        "关放风": ("close", ""),
        "停放风": ("close", ""),
        "收风吧": ("close", ""),
        "放风状态": ("status", ""),
        "查看放风": ("status", ""),
        "放风 状态": ("status", ""),
        "放风？": ("status", ""),
        # 以下都不该被当成放风指令（误伤防护）
        "放风筝": None,
        "放风了": None,
        "我要放风5分钟": None,
        "放风怎么样": None,
        "收风了没": None,
        "": None,
        "你好": None,
    }
    for text, want in cases.items():
        got = c(text)
        assert got == want, f"classify_vent({text!r}) = {got}, 期望 {want}"
    print("  ✅ classify_vent")


def test_fmt_duration():
    f = vent.fmt_duration
    assert f(30) == "30秒"
    assert f(300) == "5分钟"
    assert f(90) == "1分钟30秒"
    assert f(5400) == "1小时30分钟"
    assert f(7200) == "2小时"
    assert f(0) == "0秒"
    print("  ✅ fmt_duration")


# =============================================================== 行为
def _plugin(**overrides):
    return plugin.LLMAllowList(fake.Context(), fake.make_config(**overrides))


async def test_block_without_vent():
    """没有放风时，非白名单用户照旧被拦（回归）。"""
    fake.reset_kv()
    pl = _plugin()
    ev = fake.FakeEvent(text="你好", sender_id="222", group_id="100")
    await pl.on_message(ev)
    assert ev.call_llm is True, "非白名单用户应被拦（should_call_llm(True)）"
    assert ev.sent and "不在白名单" in ev.sent[-1], f"应收到自定义回复，实际 {ev.sent}"

    hook = fake.FakeEvent(text="你好", sender_id="222", group_id="100")
    await pl.block_non_allowlist_llm(hook, req=None)
    assert hook.is_stopped(), "on_llm_request 钩子应取消请求"

    # 白名单用户不受影响
    ok = fake.FakeEvent(text="你好", sender_id="111", group_id="100")
    await pl.on_message(ok)
    assert ok.call_llm is False and not ok.sent
    await pl.terminate()
    print("  ✅ 无放风时按白名单拦截")


async def test_vent_open_exempts_everyone():
    """管理员放风后，群内所有人都被放行；收风后立刻恢复。"""
    fake.reset_kv()
    pl = _plugin()
    key = "aiocqhttp:100"

    admin = fake.FakeEvent(text="放风5分钟", sender_id="999", group_id="100", is_admin=True)
    assert await pl._handle_vent_command(admin) is True
    assert "放风开始" in admin.sent[-1], admin.sent
    assert admin.call_llm is True, "指令本身不应再被 LLM 回答"
    left = pl.vents.left(key)
    assert 295 <= left <= 300, f"窗口应为 5 分钟，实际 {left}"

    # 非白名单用户：handler 放行 + hook 放行
    for sender in ("222", "333"):
        ev = fake.FakeEvent(text="随便聊聊", sender_id=sender, group_id="100")
        await pl.on_message(ev)
        assert ev.call_llm is False, f"{sender} 放风期间应放行"
        assert not ev.sent, "放风期间不该再发自定义回复"

        hook = fake.FakeEvent(text="随便聊聊", sender_id=sender, group_id="100")
        await pl.block_non_allowlist_llm(hook, req=None)
        assert not hook.is_stopped(), f"{sender} 放风期间 hook 不该拦截"

    # 续期
    again = fake.FakeEvent(text="放风10分钟", sender_id="999", group_id="100", is_admin=True)
    assert await pl._handle_vent_command(again) is True
    assert "延长" in again.sent[-1], again.sent
    assert 595 <= pl.vents.left(key) <= 600

    # 收风
    close = fake.FakeEvent(text="收风", sender_id="999", group_id="100", is_admin=True)
    assert await pl._handle_vent_command(close) is True
    assert "放风已结束" in close.sent[-1]
    assert pl.vents.left(key) == 0

    back = fake.FakeEvent(text="随便聊聊", sender_id="222", group_id="100")
    await pl.on_message(back)
    assert back.call_llm is True, "收风后应恢复拦截"
    await pl.terminate()
    print("  ✅ 放风放行全群 / 收风恢复")


async def test_vent_is_per_group():
    """放风只影响发指令的那个群。"""
    fake.reset_kv()
    pl = _plugin()
    admin = fake.FakeEvent(text="放风5分钟", sender_id="999", group_id="100", is_admin=True)
    await pl._handle_vent_command(admin)

    other = fake.FakeEvent(text="随便聊聊", sender_id="222", group_id="200")
    await pl.on_message(other)
    assert other.call_llm is True, "别的群不该被放风影响"
    assert other.sent and "不在白名单" in other.sent[-1]
    await pl.terminate()
    print("  ✅ 放风按群隔离")


async def test_vent_permission():
    """普通群员不能放风；管理员恒可；白名单用户看开关。"""
    fake.reset_kv()
    pl = _plugin()

    # 普通群员：指令不被接管 → 走原有拦截流程，且不会创建窗口
    ev = fake.FakeEvent(text="放风5分钟", sender_id="222", group_id="100")
    assert await pl._handle_vent_command(ev) is False, "普通群员不该能放风"
    await pl.on_message(ev)
    assert pl.vents._windows == {}, "未授权不该创建放风窗口"
    assert ev.call_llm is True and "不在白名单" in ev.sent[-1]

    # 白名单用户：默认也不能放风
    wl = fake.FakeEvent(text="放风5分钟", sender_id="111", group_id="100")
    assert await pl._handle_vent_command(wl) is False, "默认白名单用户也不能放风"
    assert pl.vents._windows == {}

    # 打开开关后白名单用户可以
    pl2 = _plugin(vent_allowlist_can_vent=True)
    wl2 = fake.FakeEvent(text="放风5分钟", sender_id="111", group_id="100")
    assert await pl2._handle_vent_command(wl2) is True
    assert "放风开始" in wl2.sent[-1]

    # 群管理员（OneBot get_group_member_list）也可以（换个群，避免撞上 pl2 的窗口）
    pl3 = _plugin(allowlist="aiocqhttp[]", admin_bypass=False)
    bot = fake.FakeBot(admins=["555"])
    gm = fake.FakeEvent(text="放风5分钟", sender_id="555", group_id="300", bot=bot)
    assert await pl3._handle_vent_command(gm) is True, "群管理员应可放风"
    assert "放风开始" in gm.sent[-1]

    await pl.terminate()
    await pl2.terminate()
    await pl3.terminate()
    print("  ✅ 放风权限（群员 / 白名单 / 管理员 / 群管）")


async def test_vent_status():
    """状态查询谁都能问。"""
    fake.reset_kv()
    pl = _plugin()
    nobody = fake.FakeEvent(text="放风状态", sender_id="222", group_id="100")
    assert await pl._handle_vent_command(nobody) is True
    assert "没有放风" in nobody.sent[-1]

    admin = fake.FakeEvent(text="放风5分钟", sender_id="999", group_id="100", is_admin=True)
    await pl._handle_vent_command(admin)
    somebody = fake.FakeEvent(text="放风状态", sender_id="222", group_id="100")
    assert await pl._handle_vent_command(somebody) is True
    assert "正在放风" in somebody.sent[-1] and "剩余" in somebody.sent[-1]
    await pl.terminate()
    print("  ✅ 放风状态查询")


async def test_vent_default_and_cap():
    """不给时长用默认值；超上限封顶。"""
    fake.reset_kv()
    pl = _plugin(vent_default_minutes=3)
    ev = fake.FakeEvent(text="放风", sender_id="999", group_id="100", is_admin=True)
    await pl._handle_vent_command(ev)
    assert 178 <= pl.vents.left("aiocqhttp:100") <= 180

    pl2 = _plugin(vent_max_minutes=1)
    ev2 = fake.FakeEvent(text="放风100分钟", sender_id="999", group_id="100", is_admin=True)
    await pl2._handle_vent_command(ev2)
    left = pl2.vents.left("aiocqhttp:100")
    assert 59 <= left <= 61, f"应封顶到 1 分钟，实际 {left}"
    assert "封顶" in ev2.sent[-1]

    pl3 = _plugin(vent_max_minutes=0)  # 0 = 不限
    ev3 = fake.FakeEvent(text="放风100分钟", sender_id="999", group_id="100", is_admin=True)
    await pl3._handle_vent_command(ev3)
    assert 5995 <= pl3.vents.left("aiocqhttp:100") <= 6000

    await pl.terminate()
    await pl2.terminate()
    await pl3.terminate()
    print("  ✅ 默认时长 / 上限封顶")


async def test_vent_expire_notice():
    """到期自动收回，并在群里提醒一句。"""
    fake.reset_kv()
    ctx = fake.Context()
    pl = plugin.LLMAllowList(ctx, fake.make_config())
    admin = fake.FakeEvent(text="放风1秒", sender_id="999", group_id="100", is_admin=True)
    await pl._handle_vent_command(admin)
    assert pl.vents.left("aiocqhttp:100") > 0

    await asyncio.sleep(1.4)
    assert pl.vents.left("aiocqhttp:100") == 0, "到期后窗口应被收回"
    assert ctx.sent, "到期应主动发一条提醒"
    assert "放风结束" in ctx.sent[-1][1], ctx.sent
    assert ctx.sent[-1][0] == admin.unified_msg_origin

    ev = fake.FakeEvent(text="你好", sender_id="222", group_id="100")
    await pl.on_message(ev)
    assert ev.call_llm is True, "到期后应恢复拦截"
    await pl.terminate()
    print("  ✅ 到期收回 + 提醒")


async def test_vent_persist():
    """放风状态写进插件 KV，插件重载后窗口仍在。"""
    fake.reset_kv()
    pl = _plugin()
    admin = fake.FakeEvent(text="放风5分钟", sender_id="999", group_id="100", is_admin=True)
    await pl._handle_vent_command(admin)
    await pl.terminate()

    pl2 = _plugin()
    assert pl2.vents._loaded is False
    ev = fake.FakeEvent(text="你好", sender_id="222", group_id="100")
    await pl2.on_message(ev)
    assert ev.call_llm is False, "重载后应恢复放风窗口"
    await pl2.terminate()
    print("  ✅ 重载后恢复放风窗口")


async def test_vent_disabled_and_victim_words():
    """功能开关关掉就完全不介入；像「放风筝」的正常聊天不被吃掉。"""
    fake.reset_kv()
    pl = _plugin(vent_enabled=False)
    admin = fake.FakeEvent(text="放风5分钟", sender_id="999", group_id="100", is_admin=True)
    assert await pl._handle_vent_command(admin) is False
    assert pl.vents._windows == {}
    await pl.terminate()

    pl2 = _plugin()
    wl = fake.FakeEvent(text="放风筝真好玩", sender_id="111", group_id="100")
    await pl2.on_message(wl)
    assert pl2.vents._windows == {}, "「放风筝」不该创建放风窗口"
    assert wl.call_llm is False, "白名单用户的普通消息不该被动过"

    blocked = fake.FakeEvent(text="放风筝真好玩", sender_id="222", group_id="100")
    await pl2.on_message(blocked)
    assert blocked.call_llm is True and "不在白名单" in blocked.sent[-1]
    await pl2.terminate()
    print("  ✅ 开关关闭 / 误伤防护")


async def test_enable_groups_regression():
    """群名单外的群完全不介入（回归 v1.0.3 行为）。"""
    fake.reset_kv()
    pl = _plugin(enable_groups="aiocqhttp[100]")
    admin_in = fake.FakeEvent(text="放风5分钟", sender_id="999", group_id="100", is_admin=True)
    assert await pl._handle_vent_command(admin_in) is True

    pl2 = _plugin(enable_groups="aiocqhttp[100]")
    admin_out = fake.FakeEvent(text="放风5分钟", sender_id="999", group_id="200", is_admin=True)
    await pl2.on_message(admin_out)
    assert pl2.vents._windows == {}, "名单外的群不该被放风"
    hook = fake.FakeEvent(text="你好", sender_id="222", group_id="200")
    await pl2.block_non_allowlist_llm(hook, req=None)
    assert not hook.is_stopped(), "名单外的群本插件不该拦截"
    await pl.terminate()
    await pl2.terminate()
    print("  ✅ 群名单回归")


async def main():
    print("放风功能测试")
    test_parse_duration()
    test_classify_vent()
    test_fmt_duration()
    await test_block_without_vent()
    await test_vent_open_exempts_everyone()
    await test_vent_is_per_group()
    await test_vent_permission()
    await test_vent_status()
    await test_vent_default_and_cap()
    await test_vent_expire_notice()
    await test_vent_persist()
    await test_vent_disabled_and_victim_words()
    await test_enable_groups_regression()
    print("\n✅ 全部通过")


if __name__ == "__main__":
    t0 = time.time()
    asyncio.run(main())
    print(f"耗时 {time.time() - t0:.2f}s")
