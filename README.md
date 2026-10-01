# 框架默认LLM回复白名单

> [English Documentation](https://github.com/Akinana22/astrbot_plugin_llmallowlist/blob/main/README.en.md)

AstrBot 插件。根据白名单控制框架默认 LLM 回复，白名单内用户正常触发 LLM，其余静默跳过或自定义回复。

## 配置

在 AstrBot WebUI 插件管理页面配置。

| 配置 | 说明 | 默认值 |
|------|------|--------|
| `admin_bypass` | 管理员自动放行 | `false` |
| `allowlist` | 白名单 | 7 个平台空 `[]` |
| `reply_msg` | 自定义回复 | 7 个平台空 `[]` |

### 填写格式

按行填写，每行格式：`平台名[内容]`。例如：

```
aiocqhttp[123456,789012]
qqofficial[234567]
telegram[987654321]
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
- 管理员自动放行依赖平台的管理员检测，目前仅 aiocqhttp（OneBot）支持。
- `reply_msg` 的自定义回复依次尝试：引用回复 → @回复 → 普通回复。
- 配置修改后需重新加载插件生效。

## 功能

- 白名单控制：仅配置中的 UID 可触发框架默认 LLM 回复
- 自定义回复：非白名单用户可按平台配置自定义回复内容
- 全平台兼容：覆盖 7 个主流群聊平台

## 消息流转

```
群消息 → llm_allowlist 插件
├─ admin_bypass 启用 且 sender 是管理员 → return，LLM 正常回复
├─ sender_id 在白名单中 → return，LLM 正常回复
└─ 不在白名单中
    ├─ 该平台有 reply_msg → 依次尝试引用/@/普通回复 → block LLM
    └─ 该平台无 reply_msg → event.should_call_llm(True)，静默跳过
```
