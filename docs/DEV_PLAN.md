# Fairy Voice 开发计划 v2.0（流式输出）

> 状态：**M5 重启（2026-08-17）**。仓库已取消归档，恢复自研协议路线。
> 目标：纯文字**流式输出**，无 TTS。核心动机是降低用户停顿感——首 token 尽快上屏。

## 1. 架构决策（2026-08-17）

- **放弃 OneBot 直连主线**：P0~P4（2026-08-14）已完成并验收，但 OneBot/aiocqhttp 路径无法做到 token 级流式（适配器按标点分段或整体缓冲），达不到「更早接到消息」的目标。保留作回退方案。
- **恢复自研 WS 协议并升级 v2.0**：C 端回到 `FairyVoiceClient`（协议 v1.1.0 代码仍在），B 端复用现有 `ws_server.py` / 记忆 / 工具链。
- **砍 TTS**：M4-3 不再实施。纯文字增量渲染（打字机效果），响应感更强、链路更短。
- **流式语义**：B 端 `Provider.text_chat_stream`（上游 AstrBot 已实现，OpenAI/Gemini/Anthropic 源均支持）逐 token 转发为 `stream_delta` 帧。

## 2. 协议 v2.0 变更（docs/PROTOCOL.md 已更新）

新增三类帧（C→B 的 ask / voice_ask 不变）：

```
B → C: {"type": "stream_begin", "id": "<uuid>", "recognized": "识别文本(可选)"}
B → C: {"type": "stream_delta", "id": "<uuid>", "delta": "增量文本"}
B → C: {"type": "stream_end",   "id": "<uuid>", "ok": true,
         "data": {"text": "完整回复文本"}}   // text 兜底，流式丢帧可恢复
```

- 错误仍走原 `response` 帧（ok=false + error）。
- `stream_begin` 后必须 `stream_end` 收尾；`stream_end` 携带完整文本，C 端以此为准落记忆/历史。
- 兼容：C 端可要求 v2.0；B 端对旧客户端仍回单帧 response（可选，V1 不实现降级）。

## 3. 里程碑

### S1 B 端流式生成（✅ 已完成 2026-08-17，commit d5e3026）
- [x] `ws_server.py`：ask / voice_ask 处理改为流式推送 stream_begin/delta/end；错误仍走 response 错误帧
- [x] `main.py`：`_handle_ask` 改用流式——`context.provider_manager.get_provider_by_id(provider_id)` 直取 Provider，调 `text_chat_stream(...)` 逐 chunk 转发（deltas.split_delta 兼容增量/快照两种 chunk 语义）
- [x] 记忆适配：`session.add_assistant` 改在 stream_end 时写入完整文本（压缩/轮数逻辑不变）
- [x] 工具模式（enable_tools）：V1 保持非流式（tool_loop_agent 完整返回后单次 yield）
- [x] 心跳保活：核实无需改动——B 端 last_seen 在收到任何 C 帧（含 ping）时刷新，流式期间 C 持续收 delta 也不会误断
- [x] `docs/PROTOCOL.md` 更新 v2.0（stream_begin/delta/end + 错误语义 + server_version 0.2.0）

### S2 C 端流式客户端（第 1~2 周，fairy-voice-android）
- [ ] `FairyVoiceClient`：`sendAsk` / `sendVoiceAsk` 增加流式回调（onStreamBegin / onStreamDelta / onStreamEnd），内部按 id 聚合；保留非流式兜底
- [ ] 流结束兜底：stream_end 未到而连接断开 → 以已收 delta 拼接 + 标记「已中断」
- [ ] `OneBotClient` 冻结为回退（不删除，主流程不再使用）；连接配置切回 fairy-voice 服务器地址/token

### S3 UI 增量渲染（第 2 周）
- [ ] `ChatMessage` / `ChatHistory`：支持流式消息（同 id 增量追加文本）
- [ ] `ChatAdapter`：增量刷新，不整体 notifyDataSetChanged；打字机效果
- [ ] 悬浮卡/通知：流式文本同步更新（对齐原 M4-1.2 悬浮窗实现，文本增量替换）

### S4 收尾（第 2~3 周）
- [ ] 状态机简化：去掉 SPEAKING/TTS 分支（VoiceController 状态 = IDLE/RECORDING/RECOGNIZING/WAITING_AI），删除 MediaPlayer 与 onTts 相关
- [x] 工具模式流式：直接迭代 ToolLoopAgentRunner.step_until_done 提取 streaming_delta（跳过 reasoning）
- [ ] 全量回归：唤醒→录音→本地 ASR→流式上屏全链路；断线重连；3 轮记忆
- [ ] 双仓库提交，Release 打包验证

### 共享 Persona prompt（✅ 已完成 2026-08-17）
- [x] `_build_system_prompt`：共享 AstrBot Persona 人设 prompt + 记忆摘要合并为 system_prompt
- [x] 配置项 `share_persona` / `persona_id`（`_conf_schema.json`）
- [x] 注入三条链路：工具模式 ProviderRequest / 非工具 text_chat_stream / 非流式 llm_generate
- [x] 人设缺失/获取失败降级为纯记忆摘要，不阻断对话

## 4. B 端关键技术点

```python
# Context 未暴露流式 API，直取 Provider（llm_generate 内部同源实现）
prov = await self.context.provider_manager.get_provider_by_id(provider_id)
async for chunk in prov.text_chat_stream(
    system_prompt=session.summary,
    contexts=contexts,
):
    # chunk.completion_text 语义不统一：增量片段 或 累计快照（流末尾回一次完整结果）
    delta, full = split_delta(full, chunk.completion_text or "")
    if delta:
        yield delta
```

- 流式 + 记忆：stream_end 的完整文本写入 `session.recent`，避免 delta 拼接脏数据。
- 心跳保活：流式推送本身即活动，`_handle_ws` 已有 last_seen 刷新逻辑，确认覆盖长任务窗口。

## 5. 验收标准

- 500 字回复：首 token 上屏 < 2s（取决于 Provider 首 token 延迟），全程增量无闪跳
- 流式中断（B 端重启/断网）：C 端 3s 内收尾，已收内容完整可读
- 记忆：stream_end 后 3 轮记忆正确，压缩/抛弃策略回归通过
- 无 TTS 残留：状态机无 SPEAKING，无 record 段解析

## 6. 仓库与提交约定

- B 端：`D:\project\fairy-voice`（remote: silver-wolf-little-wife/fairy-voice），改动随里程碑提交
- C 端：`D:\project\fairy-voice-android`（remote: silver-wolf-little-wife/fairy-voice-app），计划见 `docs/PLAN_STREAMING.md`
- 上游 AstrBot 源码：`D:\project\AstrBot`（只读参考，本方案不修改上游）
