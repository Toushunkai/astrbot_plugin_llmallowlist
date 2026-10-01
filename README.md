# 框架默认LLM回复白名单

> [English Documentation](https://github.com/Akinana22/astrbot_plugin_llmallowlist/blob/main/README.en.md)

AstrBot 插件。根据白名单控制框架默认 LLM 回复，白名单内用户正常触发 LLM，其余静默跳过或自定义回复。

## 配置

在 AstrBot WebUI 插件管理页面配置。

| 配置 | 说明 | 默认值 |
|------|------|--------|
| `enable_groups` | 启用本插件的群名单 | 空（不限制，所有群启用） |
| `admin_bypass` | 管理员自动放行（全局管理员 + 群主/群管理员） | `true` |
| `allowlist` | 白名单 | 7 个平台空 `[]` |
| `reply_msg` | 自定义回复 | 7 个平台空 `[]` |

### 填写格式

按行填写，每行格式：`平台名[内容]`。例如：

```
aiocqhttp[123456,789012]
qqofficial[234567]
telegram[987654321]
```

群名单（`enable_groups`）同样是 `平台名[群号1,群号2,...]`：

```
aiocqhttp[123456,789012]
```

| 平台名 | 说明 |
|--------|------|
| `aiocqhttp` | QQ (OneBot) |
| `qqofficial` | QQ 官方 |
| `qqofficial_webhook` | QQ 官方 Webhook |
| `telegram` | Telegram |
| `lark` | 飞书 |
| `discord` | Discord |
| `kook` | KOOK |

### 注意事项

- 白名单为空时，所有用户的 LLM 回复均被静默跳过。
- 群名单留空 = 不限制，所有群都启用本插件（向后兼容）；一旦填写，**只有名单内的群**启用本插件，
  其余群完全不拦（LLM 正常回复）。只写了某个平台的行时，其他平台的群视为未启用。
- 管理员自动放行（默认开）包含两类：
  - **AstrBot 全局管理员**：`admins_id` 里的号，`event.role == "admin"`，全平台有效；
  - **群主 / 群管理员**：依赖 OneBot 的 `get_group_member_list`，仅 aiocqhttp 平台有效（结果缓存 1 小时）。
  关闭 `admin_bypass` 后两类都不放行，严格按白名单。
- `reply_msg` 的自定义回复依次尝试：引用回复 → @回复 → 普通回复。
- 配置修改后需重新加载插件生效。

## 功能

- 群名单：只在指定群启用本插件（`enable_groups`，留空 = 全部群启用）
- 白名单控制：仅配置中的 UID 可触发框架默认 LLM 回复
- 自定义回复：非白名单用户可按平台配置自定义回复内容
- 管理员放行：AstrBot 全局管理员 + 群主/群管理员无视白名单（`admin_bypass`，默认开）
- 全平台兼容：覆盖 7 个主流群聊平台
- 兜底拦截：`on_llm_request` hook 取消非白名单发送者的**一切** LLM 请求（含内置/其他插件自发的请求）

## 消息流转

```
群消息 → llm_allowlist 插件
├─ 该群不在 enable_groups 名单内（名单非空时）→ return，本插件完全不干预
├─ admin_bypass 开 且 sender 是全局管理员 / 群主 / 群管理员 → return，LLM 正常回复
├─ sender_id 在白名单中 → return，LLM 正常回复
└─ 不在白名单中
    ├─ 该平台有 reply_msg → 依次尝试引用/@/普通回复 → block LLM
    └─ 该平台无 reply_msg → event.should_call_llm(True)，静默跳过

任意 LLM 请求（默认链路 / 内置空@ / 其他插件 request_llm）
→ on_llm_request hook（priority=maxsize）
   ├─ 非群聊 → 放行（本插件只管群聊）
   ├─ 群聊且 sender 白名单/管理员放行 → 放行
   └─ 群聊且不在白名单 → event.stop_event() → 本次 LLM 请求被取消
```

## 为什么需要 hook 兜底

`event.should_call_llm(True)` 只能阻止 **AstrBot 默认的 LLM 请求链路**（AstrBot 源码原注释：
「只会阻止 AstrBot 默认的 LLM 请求链路，不会阻止插件中的 LLM 请求」）。而 AstrBot 内置 `astrbot` 星的
`handle_empty_mention`（「空 @ 等待」，默认开启 `empty_mention_waiting_need_reply`）优先级为
`maxsize - 1`、高于本插件，它会在**只 @ 机器人**或**只发唤醒词**时自己 `yield event.request_llm(...)`，
于是非白名单用户仍能拿到一条 LLM 回复（它还会 `event.stop_event()`，导致本插件的 handler 根本没执行）。

AstrBot 在真正请求 provider 前有一道 hook 门（`agent_sub_stages/internal.py`，v4.9.2 ~ 当前版本一致）：

```python
if await call_event_hook(event, EventType.OnLLMRequestEvent, req):
    return
```

因此在 hook 里 `event.stop_event()` 即可取消本次请求，对默认链路、内置空 @、其他插件自发的
`request_llm` 一视同仁；且**不需要抢占 handler 优先级、不会阻断其他插件与自带指令**。

### 边界说明

- 只管群聊（与 `@filter.event_message_type(GROUP_MESSAGE)` 一致）；私聊不受影响。如需覆盖私聊，
  把 hook 里的 `event.get_message_type() != MessageType.GROUP_MESSAGE` 判断去掉即可。
- 直接调用 provider（如 `context.get_using_provider().text_chat(...)`）或 AstrBot 定时任务内部的
  LLM 调用不经过该 hook，不受本插件约束。
- 用 `StarTools.create_event()` 伪造群消息来触发 LLM 的插件：伪造的 sender 需要在白名单内，否则会被拦。

## 版本记录

| 版本 | 说明 |
|------|------|
| 1.0.3 | 新增 `enable_groups` 群名单：只在名单内的群启用本插件，名单外完全不拦；留空 = 全部群启用 |
| 1.0.2 | 管理员放行扩展到**群主 / 群管理员**（OneBot `get_group_member_list`）与 **AstrBot 全局管理员**（`event.role`，全平台），`admin_bypass` 默认改为 `true` |
| 1.0.1 | 新增 `on_llm_request` 兜底 hook，修复「非白名单用户只 @ 机器人 / 只发唤醒词仍能得到 LLM 回复」 |
| 1.0.0 | 首个版本：按平台白名单控制框架默认 LLM 回复 + 自定义回复 + 管理员放行 |

