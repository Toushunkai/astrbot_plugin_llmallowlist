# Framework Default LLM Reply Allowlist

> [中文文档](https://github.com/Akinana22/astrbot_plugin_llmallowlist/blob/main/README.md)

An AstrBot plugin that controls framework default LLM replies based on an allowlist. Users in the allowlist trigger LLM normally, others are silently skipped or receive a custom reply.
It also ships an "open window" (放风) mode: one message temporarily opens the whole group for a limited time.

## Configuration

Configure via the AstrBot WebUI plugin management page.

| Option | Description | Default |
|--------|-------------|---------|
| `enable_groups` | Groups where this plugin is active (empty = all groups) | empty |
| `admin_bypass` | Admin bypass (global admins + group owner/admins) | `true` |
| `allowlist` | Allowlist | 7 platforms empty `[]` |
| `reply_msg` | Custom Reply | 7 platforms empty `[]` |
| `vent_enabled` | Master switch for the open-window feature | `true` |
| `vent_allowlist_can_vent` | Let allowlisted non-admins open a window too | `false` |
| `vent_default_minutes` | Duration used when the command carries no duration | `5` |
| `vent_max_minutes` | Per-window duration cap in minutes (`0` = no cap) | `120` |
| `vent_expire_notify` | Post a notice in the group when a window expires | `true` |

### Format

One line per platform, format: `platform[content]`. Example:

```
aiocqhttp[123456,789012]
qqofficial[234567]
telegram[987654321]
```

Group list (`enable_groups`) uses `platform[group1,group2,...]`:

```
aiocqhttp[123456,789012]
```

| Platform | Description |
|----------|-------------|
| `aiocqhttp` | QQ (OneBot) |
| `qqofficial` | QQ Official |
| `qqofficial_webhook` | QQ Official Webhook |
| `telegram` | Telegram |
| `lark` | Lark (Feishu) |
| `discord` | Discord |
| `kook` | KOOK |

### Notes

- When the allowlist is empty, all LLM replies are silently skipped (except while the group is in an open window).
- Admin bypass covers two kinds: AstrBot global admins (`admins_id`, all platforms) and group owner/admins (OneBot `get_group_member_list`, aiocqhttp only, cached for 1 hour).
- Custom replies in `reply_msg` attempt: quote reply → @ reply → plain text.
- Reload the plugin after changing configuration.

## Open window (放风)

When nobody is allowlisted but the group should be able to chat for a while, an admin just says:

```
放风            → open for vent_default_minutes (5 by default)
放风5分钟        → open for 5 minutes
放风 30分钟      → spaces are free
放风1小时30分    → also 30s / 半小时 / 十分钟 / 一刻钟 / 2小时 / 1天
放风状态        → show the remaining time (anyone may ask)
收风            → close early (also 关放风 / 停放风 / 取消放风 / 结束放风)
```

- A window only affects **the group where the command was sent**; it closes automatically on expiry.
- While a window is open, **every** user in that group bypasses the allowlist — no silent skip, no
  `reply_msg` custom reply, and the `on_llm_request` hook lets those requests through as well.
- Who may open a window: AstrBot global admins and group owner/admins. Allowlisted regular users
  need `vent_allowlist_can_vent` enabled.
- Permission is checked against the allowlist/admin identity only, so a regular member cannot extend
  a window while it is open.
- Sending the command again while a window is open resets it to *now + duration*.
- Durations above `vent_max_minutes` are capped; a bare `放风` uses `vent_default_minutes`.
- Normal chat such as 放风筝 is never swallowed: the text after the keyword must parse as a duration
  (or the message must be a status/close command), otherwise it goes through the normal allowlist flow.
- Windows are persisted in the plugin KV store, so they survive plugin reloads and bot restarts.
- Turning `vent_enabled` off disables the feature entirely (even status queries).

The open-window logic lives in `vent.py` (command parsing, duration parsing and the window
table; it does not import AstrBot, so it is unit-testable), while `main.py` only keeps the
plugin glue (permissions, replies, handlers). Run the tests with `python3 _tests/run_all.py`.

## Features

- Group list: only activate in the configured groups (`enable_groups`, empty = all groups)
- Allowlist control: only configured UIDs trigger the framework default LLM reply
- Custom reply: non-allowlisted users can receive platform-specific custom replies
- Admin bypass: AstrBot global admins + group owner/admins bypass the allowlist (`admin_bypass`)
- Open window: `放风5分钟` opens the group for everyone until it expires or someone sends `收风`
- Cross-platform: covers 7 mainstream group chat platforms
- Hook fallback: the `on_llm_request` hook cancels **every** LLM request from non-allowlisted senders
  (including requests made by built-in stars or other plugins)

## Message Flow

```
Group message → llm_allowlist plugin
├─ group not in enable_groups (when non-empty) → return, plugin does not interfere
├─ open-window command (放风X / 收风 / 放风状态)
│   ├─ status → reply with remaining time (anyone may ask)
│   ├─ permitted (admin, or allowlisted user with vent_allowlist_can_vent)
│   │   ├─ 放风X → open window + expiry task → confirm → block default LLM chain
│   │   └─ 收风  → close window → confirm → block default LLM chain
│   └─ not permitted → treat as a normal message and continue below
├─ admin_bypass enabled and sender is admin → return, LLM replies normally
├─ sender_id in allowlist → return, LLM replies normally
├─ group window is open → return, LLM replies normally
└─ not in allowlist
    ├─ platform has reply_msg → try quote/@/plain → block LLM
    └─ platform has no reply_msg → event.should_call_llm(True), silent skip

Any LLM request (default chain / empty-@ / other plugins)
→ on_llm_request hook (priority=maxsize)
   ├─ not a group message → allow
   ├─ group message, sender allowlisted/bypassed → allow
   ├─ group message, window open → allow
   └─ group message, sender not allowlisted → event.stop_event() → request cancelled
```
