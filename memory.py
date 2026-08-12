# SPDX-License-Identifier: AGPL-3.0-only
"""Fairy Voice 记忆策略：3 轮原文 + 超轮压缩摘要 + TTL 过期抛弃。

规则（按 device_id 维护）：
- 保留最近 ``rounds`` 轮原文（1 轮 = user + assistant 2 条消息）。
- 超出后若在 TTL 内再次对话，最早轮次交给 LLM 压缩为摘要（由插件层执行）。
- 超过 TTL 未对话，清空全部记忆（含摘要），视为新会话。
"""

import time

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


class MemorySession:
    """单个设备的记忆会话。"""

    def __init__(self, ttl: float = 300.0, rounds: int = 3):
        self.ttl = ttl
        self.rounds = rounds
        self.summary: str | None = None  # 压缩后的长期记忆摘要
        self.recent: list[dict] = []  # [{"role": ..., "content": ...}]
        self.last_active: float = time.monotonic()

    @property
    def expired(self) -> bool:
        """超过 TTL 未活动即过期（抛弃记忆）。"""
        return time.monotonic() - self.last_active > self.ttl

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def add_user(self, text: str) -> None:
        self.touch()
        self.recent.append({"role": ROLE_USER, "content": text})

    def add_assistant(self, text: str) -> None:
        self.touch()
        self.recent.append({"role": ROLE_ASSISTANT, "content": text})

    def needs_compress(self) -> bool:
        """超出 rounds 轮（rounds*2 条消息）需要压缩。"""
        return len(self.recent) > self.rounds * 2

    def pop_oldest_round(self) -> list[dict]:
        """取出最早一轮（2 条）用于压缩，返回被移除的消息。"""
        old = self.recent[:2]
        self.recent = self.recent[2:]
        return old

    def set_summary(self, summary: str) -> None:
        self.summary = summary

    def clear(self) -> None:
        """抛弃全部记忆（含摘要），保持会话对象存活。"""
        self.summary = None
        self.recent = []
        self.touch()

    def describe(self) -> dict:
        return {
            "summary": self.summary,
            "recent_rounds": len(self.recent) // 2,
            "expired": self.expired,
        }


class MemoryManager:
    """按 device_id 管理多个记忆会话。"""

    def __init__(self, ttl: float = 300.0, rounds: int = 3):
        self.ttl = ttl
        self.rounds = rounds
        self._sessions: dict[str, MemorySession] = {}

    def get(self, device_id: str) -> MemorySession:
        """获取设备会话；不存在则新建，已过期则清空重开。"""
        s = self._sessions.get(device_id)
        if s is None:
            s = MemorySession(ttl=self.ttl, rounds=self.rounds)
            self._sessions[device_id] = s
        elif s.expired:
            s.clear()
        return s

    def drop(self, device_id: str) -> None:
        self._sessions.pop(device_id, None)

    def summary(self) -> dict:
        return {did: s.describe() for did, s in self._sessions.items()}
