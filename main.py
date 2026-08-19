# SPDX-License-Identifier: AGPL-3.0-only
"""Fairy Voice —— AstrBot 手机语音助手接入插件。

B 端：
- 内嵌 aiohttp WebSocket 服务端，接受手机 App（fairy-voice-app）主动外连。
- 收到 ask 后按 device_id 记忆策略（3 轮 + 5 分钟压缩/抛弃）组装上下文，
  调用 AstrBot LLM（Provider.text_chat_stream 流式 / tool_loop_agent 流式）→ 流式回传 AI 回复。
- 协议 v2.0（docs/PROTOCOL.md）：stream_begin / stream_delta / stream_end，无 TTS。
- ASR 已移至 C 端（sherpa-onnx 本地识别），voice_ask 已弃用。

会话与工具：
- 每个 device_id 是独立会话（B 端内存记忆），不绑定真实聊天，umo = fairy_voice:friend:{device_id}。
- 无真实 event 时构造最小 AstrMessageEvent（三个类均可无平台构造，已源码验证），
  因此 tool_loop_agent（自动工具循环）可用；工具注册表全局共享，米家/远程电脑
  已注册工具均可被语音指令调用（enable_tools 开启后）。
"""

import asyncio
import uuid

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.agent.message import (
        AssistantMessageSegment,
        TextPart,
        UserMessageSegment,
    )
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.message.components import Plain
    try:
        from astrbot.core.platform.message_type import MessageType  # v4.27.2+
    except ImportError:  # pragma: no cover —— 旧版本路径
        from astrbot.core.message.message_type import MessageType
    from astrbot.core.platform.astr_message_event import AstrMessageEvent as _BaseEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.provider.provider import Provider
except ImportError:  # pragma: no cover —— 兼容旧版本
    _BaseEvent = None  # type: ignore
    AssistantMessageSegment = None  # type: ignore
    TextPart = None  # type: ignore
    UserMessageSegment = None  # type: ignore
    ToolSet = None  # type: ignore
    Provider = None  # type: ignore

from .deltas import split_delta
from .memory import ROLE_ASSISTANT, ROLE_USER, MemoryManager
from .ws_server import FairyWsServer

COMPRESS_PROMPT = (
    "请将以下对话压缩为要点摘要，保留用户偏好、关键事实与未完成任务，控制在 100 字以内：\n{content}"
)

PLATFORM_ID = "fairy_voice"


@register(
    "astrbot_plugin_fairy_voice",
    "chengxiyue",
    "手机语音助手接入器：接收 fairy-voice-app 语音指令，调用 AstrBot LLM 流式处理并回传",
    "0.2.0",
)
class FairyVoice(Star):
    """Fairy Voice —— 手机语音助手接入插件。"""

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.server: FairyWsServer | None = None
        self._server_task: asyncio.Task | None = None
        self.memory = MemoryManager(
            ttl=float(config.get("memory_ttl", 300)),
            rounds=int(config.get("memory_rounds", 3)),
        )
        self._enable_tools = bool(config.get("enable_tools", False))
        self._tool_max_steps = int(config.get("tool_max_steps", 10))

    async def initialize(self) -> None:
        """启动 WebSocket 服务端。"""
        port = int(self.config.get("ws_port", 8766))
        token = str(self.config.get("auth_token", ""))
        heartbeat_timeout = int(self.config.get("heartbeat_timeout", 60))

        self.server = FairyWsServer(
            port=port,
            token=token,
            heartbeat_timeout=heartbeat_timeout,
            ask_handler=self._handle_ask,
            ask_stream_handler=self._handle_ask_stream,
        )
        self._server_task = asyncio.create_task(self.server.start())
        logger.info("Fairy Voice 插件初始化完成（协议 v2.0 流式）")

    # ---------- ask 处理核心（v2.0 流式） ----------

    async def _handle_ask_stream(self, device_id: str, text: str):
        """流式 ask：记忆策略 → Provider.text_chat_stream 逐增量 yield → 回写记忆。

        yield 语义：每个元素是增量文本片段；全部结束后由 ws_server 聚合并发 stream_end。
        assistant 记忆以完整文本写入，避免 delta 拼接脏数据。
        工具模式（enable_tools）V1 保持非流式：tool_loop_agent 完整返回后单次 yield。
        """
        event = self._build_event(device_id, text)
        provider_id = await self._resolve_provider_id(event.unified_msg_origin)
        session = self.memory.get(device_id)
        session.add_user(text)

        # 超出 rounds 轮：把最早一轮压缩进摘要（无需工具）
        if session.needs_compress():
            old = session.pop_oldest_round()
            content = "\n".join(f"{m['role']}: {m['content']}" for m in old)
            try:
                sum_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=COMPRESS_PROMPT.format(content=content),
                )
                session.set_summary(sum_resp.completion_text.strip())
            except Exception as e:  # noqa: BLE001 —— 压缩失败不阻断对话
                logger.warning(f"记忆压缩失败（继续对话）: {e}")

        contexts = [
            self._build_message(m["role"], m["content"]) for m in session.recent
        ]

        full = ""
        if self._enable_tools and ToolSet is not None:
            # 工具模式：直接迭代 step_until_done 获得流式增量
            # （tool_loop_agent 吞掉了流式增量，只返回最终结果）
            from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
            from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
            from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
            from astrbot.core.provider.entities import ProviderRequest

            prov = await self.context.provider_manager.get_provider_by_id(provider_id)
            if prov is None:
                raise RuntimeError(f"Provider {provider_id} not found")

            agent_runner = ToolLoopAgentRunner()
            tool_executor = FunctionToolExecutor()
            # context 字段需要 Context 实例（插件里是 self.context，Star 构造时注入），不能传 self（FairyVoice）
            agent_context = AstrAgentContext(context=self.context, event=event)

            request = ProviderRequest(
                prompt=text,
                func_tool=self._all_tools(),
                contexts=[m.model_dump() if hasattr(m, 'model_dump') else m for m in contexts],
                system_prompt=session.summary or "",
            )

            await agent_runner.reset(
                provider=prov,
                request=request,
                run_context=AgentContextWrapper(
                    context=agent_context,
                    tool_call_timeout=120,
                ),
                tool_executor=tool_executor,
                streaming=True,  # 启用流式
            )

            async for response in agent_runner.step_until_done(self._tool_max_steps):
                if response.type == "streaming_delta" and response.data and response.data.chain:
                    # 提取增量文本
                    delta_text = ""
                    for seg in response.data.chain.chain:
                        if hasattr(seg, 'text') and seg.text:
                            delta_text += seg.text
                    if delta_text:
                        full += delta_text
                        yield delta_text
        else:
            prov = await self.context.provider_manager.get_provider_by_id(provider_id)
            if prov is None or not self._provider_supports_stream(prov):
                # 降级：非流式 Provider（基类未实现 text_chat_stream）
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=session.summary,
                    contexts=contexts,
                )
                full = resp.completion_text
                if full:
                    yield full
            else:
                async for chunk in prov.text_chat_stream(
                    system_prompt=session.summary,
                    contexts=contexts,
                ):
                    delta, full = split_delta(full, chunk.completion_text or "")
                    if delta:
                        yield delta
        session.add_assistant(full)

    async def _handle_ask(self, device_id: str, text: str) -> str:
        """非流式兜底：记忆策略 → LLM 生成（可选工具循环）→ 回写记忆。

        ws_server 优先使用 ask_stream_handler，此方法仅作流式不可用时的回退。
        """
        event = self._build_event(device_id, text)
        provider_id = await self._resolve_provider_id(event.unified_msg_origin)
        session = self.memory.get(device_id)
        session.add_user(text)

        # 超出 rounds 轮：把最早一轮压缩进摘要（无需工具）
        if session.needs_compress():
            old = session.pop_oldest_round()
            content = "\n".join(f"{m['role']}: {m['content']}" for m in old)
            try:
                sum_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=COMPRESS_PROMPT.format(content=content),
                )
                session.set_summary(sum_resp.completion_text.strip())
            except Exception as e:  # noqa: BLE001 —— 压缩失败不阻断对话
                logger.warning(f"记忆压缩失败（继续对话）: {e}")

        contexts = [
            self._build_message(m["role"], m["content"]) for m in session.recent
        ]

        if self._enable_tools and ToolSet is not None:
            resp = await self.context.tool_loop_agent(
                event=event,
                chat_provider_id=provider_id,
                contexts=contexts,
                tools=self._all_tools(),
                max_steps=self._tool_max_steps,
            )
        else:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=session.summary,
                contexts=contexts,
            )
        reply = resp.completion_text
        session.add_assistant(reply)
        return reply

    @staticmethod
    def _provider_supports_stream(prov) -> bool:
        """Provider 子类是否真正实现了 text_chat_stream（基类为 raise NotImplementedError）。"""
        if Provider is None:
            return hasattr(prov, "text_chat_stream")
        return type(prov).text_chat_stream is not Provider.text_chat_stream

    async def _resolve_provider_id(self, umo: str) -> str:
        """先按会话 umo 取（支持会话隔离），失败回退全局默认。"""
        for candidate in (umo, None):
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=candidate)
            except Exception:
                continue
            if provider_id:
                return provider_id
        raise RuntimeError("未配置可用 LLM Provider")

    # ---------- 最小 event 构造（无真实平台事件） ----------

    def _build_event(self, device_id: str, text: str) -> _BaseEvent:
        """构造最小 AstrMessageEvent，umo = fairy_voice:friend:{device_id}。"""
        if _BaseEvent is None:
            raise RuntimeError("AstrBot 版本过旧，缺少 platform.astr_message_event 模块")
        msg = AstrBotMessage()
        msg.type = MessageType.FRIEND_MESSAGE
        msg.self_id = PLATFORM_ID
        msg.session_id = device_id
        msg.message_id = f"fairy-{uuid.uuid4()}"
        msg.sender = MessageMember(user_id=device_id, nickname="FairyVoice")
        msg.message = [Plain(text=text)]
        msg.message_str = text
        meta = PlatformMetadata(
            name=PLATFORM_ID,
            description="Fairy Voice 语音终端",
            id=PLATFORM_ID,
        )
        return _BaseEvent(
            message_str=text,
            message_obj=msg,
            platform_meta=meta,
            session_id=device_id,
        )

    def _build_message(self, role: str, content: str):
        """把记忆消息转为 AstrBot Message segment。"""
        if AssistantMessageSegment is None or TextPart is None or UserMessageSegment is None:
            raise RuntimeError("AstrBot 版本过旧，缺少 agent.message 模块")
        cls = UserMessageSegment if role == ROLE_USER else AssistantMessageSegment
        return cls(content=[TextPart(text=content)])

    def _all_tools(self) -> ToolSet | None:
        """收集全局注册的全部工具（插件工具 + MCP + 内置工具）。"""
        try:
            mgr = self.context.get_llm_tool_manager()
            tools = list(mgr.func_list) + list(mgr.builtin_func_list.values())
            return ToolSet(tools=tools)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"获取工具列表失败: {e}")
            return None

    # ---------- 命令 ----------

    @filter.command("fairy")
    async def fairy(self, event: AstrMessageEvent):
        """Fairy Voice 状态查询。发送 /fairy 查看在线设备与记忆状态。"""
        if self.server is None:
            yield event.plain_result("Fairy Voice 尚未初始化。")
            return
        devices = self.server.device_summary()
        if not devices:
            yield event.plain_result(
                "Fairy Voice 已就绪，但暂无手机设备在线。请启动 fairy-voice-app 并接入。"
            )
            return
        lines = [
            f"- {d['device_id']}（session {d['session_id'][:8]}"
            + ("，工具模式开启" if self._enable_tools else "")
            + "）"
            for d in devices
        ]
        mem = self.memory.summary()
        for did, info in mem.items():
            lines.append(
                f"  - 记忆: 摘要={'有' if info['summary'] else '无'}，"
                f"最近 {info['recent_rounds']} 轮，过期={'是' if info['expired'] else '否'}"
            )
        yield event.plain_result("Fairy Voice 已就绪，在线设备：\n" + "\n".join(lines))

    async def terminate(self) -> None:
        """插件卸载/停用时：停止服务端。"""
        if self.server:
            await self.server.stop()
        logger.info("Fairy Voice 插件已停止。")
