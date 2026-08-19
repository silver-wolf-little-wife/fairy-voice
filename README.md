# fairy-voice

fairy-voice 语音助手的 **B 端 AstrBot 插件**（仓库根即插件根）。

协议 v2.0 流式输出，配合 C 端 [fairy-voice-app](https://github.com/silver-wolf-little-wife/fairy-voice-app) 使用。
ASR 已移至 C 端本地（sherpa-onnx），B 端只处理 LLM 流式生成与工具调用。

## 架构

```
C 端（Android）                    B 端（本仓库，AstrBot 插件）
FairyVoiceClient                   FairyWsServer
  ask 帧 ──────────────────────▶     ↓ 记忆策略 + Provider.text_chat_stream
                                       ↓ 工具循环（tool_loop_agent）
  stream_begin ◀────────────────     ↓ 流式推送
  stream_delta ◀────────────────     ↓ 逐增量
  stream_delta ◀────────────────     ↓
  stream_end ◀──────────────────     ↓ 完整文本兜底
```

## 目录

```
fairy-voice/                  ← 仓库根 = 插件根（插件名 astrbot_plugin_fairy_voice）
├─ __init__.py                插件入口（FairyVoice Star）
├─ main.py                    主逻辑：LLM 流式生成 / Agent 工具循环 / 记忆策略
├─ ws_server.py               aiohttp WebSocket 服务端（hello/心跳/ask/流式推送）
├─ memory.py                  3 轮 + 5 分钟记忆策略
├─ deltas.py                  流式增量切割（兼容增量片段与累计快照两种 Provider 语义）
├─ docs/                      协议与计划文档
└─ tests/                     单元测试
```

## 安装

1. 克隆本仓库，将**仓库内容**放入 AstrBot 的 `data/plugins/astrbot_plugin_fairy_voice/`
2. 重启 AstrBot 或重载插件，在插件配置中设置：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ws_port` | 8766 | WebSocket 服务端口 |
| `auth_token` | change-me | 握手认证 token（C 端需配相同值） |
| `heartbeat_timeout` | 60 | 心跳超时（秒），超时清理设备与记忆 |
| `memory_ttl` | 300 | 记忆保留时长（秒），超过抛弃全部记忆 |
| `memory_rounds` | 3 | 保留最近对话轮数，超出后最早轮次压缩为摘要 |
| `enable_tools` | false | 启用工具调用（流式 tool_loop_agent） |
| `tool_max_steps` | 10 | 工具循环最大步数 |
| `share_persona` | false | 共享 AstrBot Persona 人设 prompt（注入 system_prompt），让语音对话遵循 AstrBot 人设 |
| `persona_id` | 空 | 指定 Persona 名称（按 name）；留空用 AstrBot 默认人设 |

3. 发送 `/fairy` 可查看在线设备与记忆状态

## 记忆策略

- 仅保留最近 3 轮对话原文
- 超出 3 轮且 5 分钟内再次对话：最早轮次交给 LLM 压缩为摘要（注入 system_prompt）
- 超过 5 分钟未对话：抛弃全部记忆，视为新会话
- 记忆只存内存，B 端重启即清空
- assistant 原文以 `stream_end` 的完整文本写入，不采用 delta 拼接

## 测试

```bash
python tests/test_memory.py    # 记忆策略
python tests/test_ws_smoke.py  # WS 冒烟（握手/心跳/ask 流式往返）
```

## 协议

见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)。v2.0 流式：stream_begin / stream_delta / stream_end，无 TTS。
voice_ask 已弃用（ASR 已移至 C 端），C 端应直接发 ask 文本帧。

## 许可

AGPL-3.0-only
