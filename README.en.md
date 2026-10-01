# Framework Default LLM Reply Allowlist

> [中文文档](https://github.com/Akinana22/astrbot_plugin_llmallowlist/blob/main/README.md)

An AstrBot plugin that controls framework default LLM replies based on an allowlist. Users in the allowlist trigger LLM normally, others are silently skipped or receive a custom reply.

## Configuration

Configure via the AstrBot WebUI plugin management page.

| Option | Description | Default |
|--------|-------------|---------|
| `admin_bypass` | Admin Bypass | `false` |
| `allowlist` | Allowlist | 7 platforms empty `[]` |
| `reply_msg` | Custom Reply | 7 platforms empty `[]` |

### Format

One line per platform, format: `platform[content]`. Example:

```
aiocqhttp[123456,789012]
qqofficial[234567]
telegram[987654321]
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

- When the allowlist is empty, all LLM replies are silently skipped.
- Admin bypass relies on platform-level admin detection, currently only supported on aiocqhttp (OneBot).
- Custom replies in `reply_msg` attempt: quote reply → @ reply → plain text.
- Reload the plugin after changing configuration.

## Features

- Allowlist control: only configured UIDs trigger the framework default LLM reply
- Custom reply: non-allowlisted users can receive platform-specific custom replies
- Cross-platform: covers 7 mainstream group chat platforms

## Message Flow

```
Group message → llm_allowlist plugin
├─ admin_bypass enabled and sender is admin → return, LLM replies normally
├─ sender_id in allowlist → return, LLM replies normally
└─ not in allowlist
    ├─ platform has reply_msg → try quote/@/plain → block LLM
    └─ platform has no reply_msg → event.should_call_llm(True), silent skip
```
