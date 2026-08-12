# fairy-voice

> 双仓库协作项目：手机语音助手接入 AstrBot。

## 架构

```
[手机 fairy-voice-app]  --WebSocket-->  [B端 AstrBot 插件 astrbot_plugin_fairy_voice]
     悬浮窗按钮/录音/识别                          LLM 生成 / Agent 工具循环
     TTS 播报 / 气泡显示  <--AI 回复--  (llm_generate / tool_loop_agent)
```

- **C 端**：Android App（Kotlin），按钮触发式语音助手，主动外连 B 端（穿透 NAT）
- **B 端**：AstrBot 插件，内嵌 WebSocket 服务端，接收手机语音指令文本，调用 AstrBot LLM/Agent 处理，回传结果
- **协议**：见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)，双端共享，保持同步

## 目录

```
fairy-voice/
├─ astrbot_plugin_fairy_voice/   B 端 AstrBot 插件
├─ fairy-voice-app/              C 端 Android App（Kotlin，开发中）
├─ tests/                        单元测试与 WS 冒烟测试
└─ docs/                         协议与开发计划
```

## B 端插件安装（astrbot_plugin_fairy_voice）

1. 克隆本仓库，将 `astrbot_plugin_fairy_voice/` 目录放入 AstrBot 的 `data/plugins/` 下
2. 重启 AstrBot 或重载插件，在插件配置中设置：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ws_port` | 8766 | WebSocket 服务端口 |
| `auth_token` | change-me | 握手认证 token（C 端 App 需配置相同值） |
| `heartbeat_timeout` | 60 | 心跳超时（秒），超时清理设备与记忆 |
| `memory_ttl` | 300 | 记忆保留时长（秒），超过抛弃全部记忆 |
| `memory_rounds` | 3 | 保留最近对话轮数，超出后最早轮次压缩为摘要 |
| `enable_tools` | false | 启用工具调用（tool_loop_agent），实机验证后建议开启 |
| `tool_max_steps` | 10 | 工具循环最大步数 |

3. 发送 `/fairy` 可查看在线设备与记忆状态

## 记忆策略

- 仅保留最近 3 轮对话原文
- 超出 3 轮且 5 分钟内再次对话：最早轮次交给 LLM 压缩为摘要（注入 system_prompt）
- 超过 5 分钟未对话：抛弃全部记忆，视为新会话

## 状态

- ✅ M1 协议定稿（`docs/PROTOCOL.md` v1.0.0）
- ✅ M2 B 端插件骨架（WS 服务端 / 记忆策略 / LLM 接入 / 工具调用支持）
- ⬜ M3 C 端 Android App
- ⬜ M4 语音闭环（录音 / 识别 / TTS）
- ⬜ M5 打磨与安全

开发计划见 [`docs/DEV_PLAN.md`](docs/DEV_PLAN.md)。
