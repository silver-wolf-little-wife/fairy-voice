# SPDX-License-Identifier: AGPL-3.0-only
"""memory.py 记忆策略单元测试。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory import ROLE_ASSISTANT, ROLE_USER, MemoryManager  # noqa: E402


def test_three_rounds_no_compress():
    mgr = MemoryManager(ttl=300, rounds=3)
    s = mgr.get("dev-1")
    for i in range(3):  # 3 轮 = 6 条消息
        s.add_user(f"q{i}")
        s.add_assistant(f"a{i}")
        assert not s.needs_compress(), f"第 {i+1} 轮不应触发压缩"
    assert len(s.recent) == 6
    assert s.summary is None


def test_fourth_round_compress():
    mgr = MemoryManager(ttl=300, rounds=3)
    s = mgr.get("dev-1")
    for i in range(3):  # 先凑满 3 轮（6 条）
        s.add_user(f"q{i}")
        s.add_assistant(f"a{i}")
    # 第 4 轮第一条 user 消息 → 7 条，触发压缩
    s.add_user("q3")
    assert s.needs_compress()
    old = s.pop_oldest_round()
    assert old == [{"role": ROLE_USER, "content": "q0"}, {"role": ROLE_ASSISTANT, "content": "a0"}]
    assert len(s.recent) == 5  # 弹出 1 轮后剩 2.5 轮
    assert s.recent[0] == {"role": ROLE_USER, "content": "q1"}
    s.set_summary("摘要")
    assert s.summary == "摘要"
    s.add_assistant("a3")  # 回复回写 → 恢复 3 轮（6 条）
    assert len(s.recent) == 6
    assert not s.needs_compress()


def test_expire_clears_memory():
    mgr = MemoryManager(ttl=0.05, rounds=3)
    s = mgr.get("dev-1")
    s.add_user("q0")
    s.add_assistant("a0")
    s.set_summary("摘要")
    time.sleep(0.08)
    s2 = mgr.get("dev-1")  # 过期后 get 应清空
    assert s2 is s
    assert s2.summary is None
    assert s2.recent == []


def test_expire_flag():
    mgr = MemoryManager(ttl=0.05, rounds=3)
    s = mgr.get("dev-1")
    s.add_user("q0")
    assert not s.expired
    time.sleep(0.08)
    assert s.expired


def test_drop_device():
    mgr = MemoryManager(ttl=300, rounds=3)
    s = mgr.get("dev-1")
    s.add_user("q0")
    mgr.drop("dev-1")
    s2 = mgr.get("dev-1")
    assert s2 is not s
    assert s2.recent == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"共 {len(fns)} 项测试全部通过")
