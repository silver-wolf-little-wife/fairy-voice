# SPDX-License-Identifier: AGPL-3.0-only
"""Fairy Voice —— AstrBot 手机语音助手接入插件。

B 端：
- 内嵌 aiohttp WebSocket 服务端，接受手机 App（fairy-voice-app）主动外连。
- 收到 ask（语音指令文本）→ 按 device_id 记忆策略（3 轮 + 5 分钟压缩/抛弃）组装上下文
  → 调用 AstrBot LLM（context.llm_generate / tool_loop_agent）→ 回传 AI 回复文本。

会话与工具：
- 每个 device_id 是独立会话（B 端内存记忆），不绑定真实聊天，umo 为 fairy_voice:friend:{device_id}。
- 无真实 event 时构造最小 AstrMessageEvent（三个类均可无平台构造，已源码验证），
  因此 tool_loop_agent（自动工具循环）可用；工具注册表全局共享，米家/远程电脑等
  已注册工具均可被语音指令调用（enable_tools 开启后）。

协议见 docs/PROTOCOL.md。
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
    from astrbot.core.message.message_type import MessageType
    from astrbot.core.platform.astr_message_event import AstrMessageEvent as _BaseEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.platform_metadata import PlatformMetadata
except ImportError:  # pragma: no cover —— 兼容旧版本
    _BaseEvent = None  # type: ignore
    AssistantMessageSegment = None  # type: ignore
    TextPart = None  # type: ignore
    UserMessageSegment = None  # type: ignore
    ToolSet = None  # type: ignore

from .memory import ROLE_ASSISTANT, ROLE_USER, MemoryManager
from .ws_server import FairyWsServer

COMPRESS_PROMPT = (
    "请将以下对话压缩为要点摘要，保留用户偏好、关键事实与未完成任务，控制在 100 字以内：\n{content}"
)

PLATFORM_ID = "fairy_voice"


@register(
    "astrbot_plugin_fairy_voice",
    "chengxiyue",
    "手机语音助手接入器：接收 fairy-voice-app 语音指令，调用 AstrBot LLM 处理并回传",
    "0.1.0",
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
        )
        self._server_task = asyncio.create_task(self.server.start())
        logger.info("Fairy Voice 插件初始化完成。")

    # ---------- ask 处理核心 ----------

    async def _handle_ask(self, device_id: str, text: str) -> str:
        """记忆策略 → LLM 生成（可选工具循环）→ 回写记忆。"""
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
        """把记忆消息转成 AstrBot Message segment。"""
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
            f"- {d['device_id']}（session {d['session_id'][:8]}）"
            + ("，工具模式开启" if self._enable_tools else "")
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
