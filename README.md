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
├─ fairy-voice-app/              C 端 Android App（Kotlin）
└─ docs/                         协议与开发计划
```

## 状态

开发计划见 [`docs/DEV_PLAN.md`](docs/DEV_PLAN.md)。
