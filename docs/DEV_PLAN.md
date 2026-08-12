# fairy-voice 开发计划

> 目标：按钮触发式手机语音助手，通过 WebSocket 接入 AstrBot，由 AstrBot 的 LLM/Agent 处理语音指令并回传结果。
> 架构参考：cherry-astrbot（B 端插件） + cherry-remote-app（C 端执行器）的双仓库协作模式。

## 整体架构

```
C端（手机 App，Kotlin）                         B端（AstrBot 插件，Python）
┌──────────────────────────┐                 ┌──────────────────────────┐
│ 悬浮窗按钮                │                 │ aiohttp WS 服务端         │
│   ↓ 按下                  │   WS 主动外连    │   ↓ hello/token 握手      │
│ 录音 + SpeechRecognizer   │ ──────────────▶ │   ↓ 收到 ask 请求         │
│   ↓ 识别文本               │    JSON 帧      │ llm_generate /           │
│ WS 客户端发送 ask          │                 │ tool_loop_agent          │
│   ↓                       │ ◀────────────── │   ↓ AI 结果回传           │
│ TTS 播报 + 气泡显示        │    response     │                          │
└──────────────────────────┘                 └──────────────────────────┘
```

- 传输：WebSocket，C 端主动外连 B 端（穿透 NAT），复用 cherry 的 hello/心跳/request-response 帧模式
- 角色：C = 语音采集与播报（纯终端），B = AI 大脑（LLM 生成 + Agent 工具循环）
- 与 cherry 的区别：cherry 的 C 端是"执行器"，本项目的 C 端是"语音终端"；B 端新增"把手机指令喂给 AstrBot LLM"的环节

## 里程碑

### M1 协议定稿（第 1 周）
- [ ] 设计 fairy-voice 协议 v1.0.0：hello 握手（token + device_id）、心跳、ask 请求/响应
- [ ] 帧格式：`{"type":"ask","id":...,"text":"...","lang":"zh-CN"}` → `{"type":"response","id":...,"ok":true,"data":{"text":"AI回复","audio_hint":true}}`
- [ ] 文档写入 docs/PROTOCOL.md，双端同步

### M2 B 端插件骨架（第 2 周）
- [ ] `astrbot_plugin_fairy_voice/`：metadata.yaml + _conf_schema.json（ws_port / auth_token / heartbeat_timeout）
- [ ] aiohttp WS 服务端：连接管理、token 校验、心跳超时清理（参考 cherry-astrbot/ws_server.py）
- [ ] 核心：收到 ask → 按 device_id 绑定独立会话（conversation_manager），用户消息写入历史 → `llm_generate(contexts=历史)` 带上下文生成 → 回复 `add_message_pair` 回写，上下文自然累积（等同普通聊天）
- [ ] 进阶：`tool_loop_agent` 支持工具调用（后续可挂 mihome/远程电脑等工具）
- [ ] 命令：`/fairy` 查看在线设备与状态

### M3 C 端 App 骨架（第 3 周）
- [ ] Android 工程（Kotlin + Compose，minSdk 26）
- [ ] 前台服务 + 常驻通知（保活），悬浮窗按钮（可拖动）
- [ ] 权限：RECORD_AUDIO / SYSTEM_ALERT_WINDOW / FOREGROUND_SERVICE / POST_NOTIFICATIONS
- [ ] WS 客户端（OkHttp WebSocket）：连接配置页（地址/token/设备名）、断线自动重连
- [ ] 联调：按钮 → 发文本 → 收 AI 回复 → 悬浮窗气泡显示

### M4 语音闭环（第 4 周）
- [ ] 录音 + SpeechRecognizer（系统识别，离线可用，按需申请权限）
- [ ] 完整链路：按下 → 录音 → 识别 → WS 发送 → AI 回复 → TTS 播报
- [ ] 不做录音连续问答 UI（按一次答一次），但 AI 侧保留上下文记忆（复用 AstrBot 会话历史，等同普通聊天）
- [ ] 状态机：空闲/录音中/识别中/等待AI/播报中，悬浮窗显示状态
- [ ] 唤醒词预留：后续可在录音链路前加轻量唤醒（可选项）

### M5 打磨与安全（第 5 周）
- [ ] 国产 ROM 保活引导页（电池白名单）
- [ ] TTS 语速/音量设置、识别语言切换
- [ ] token 加密存储（EncryptedSharedPreferences）、WS 支持 wss
- [ ] 打包签名 APK/AAB

## B 端关键技术点（已确认，来自 AstrBot 官方文档）

```python
# 获取当前会话的 provider
provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)

# 直接调用 LLM（v4.5.7+）
llm_resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt="...")
text = llm_resp.completion_text

# 带工具的 Agent 循环
resp = await self.context.tool_loop_agent(
    event=event, chat_provider_id=provider_id,
    prompt="...", tools=ToolSet([...]), max_steps=30,
)
```

> 注意：WS 请求没有真实 `event`，需要构造最小 event 或使用插件默认会话的 umo 获取 provider_id；M2 阶段验证此路径。

## 验收标准

- 手机按按钮 → 3 秒内开始录音，识别文本 3 秒内返回
- WS 断线 15 秒内自动重连，重连后状态同步
- AI 回复从发送到 TTS 播报 < 10 秒（视模型速度）
- 后台挂机 24 小时耗电 < 2%
- token 错误握手即断，无明文存储

## 参考

- 插件架构与 WS 服务端：`D:\project\cherry-astrbot`
- 协议模式：`cherry-astrbot/docs/PROTOCOL.md`
- AstrBot 插件 API：`D:\project\AstrBot\docs\zh\dev\star\guides\ai.md`
