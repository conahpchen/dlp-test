"""Cowrie 桥接：映射正确、合规（/24 截断 + 密码即时哈希）、schema 合法（契约 12 §1）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from entries.cowrie.bridge import main
from replay.harness import load_events, validate_events

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]
SAMPLE = FIXTURES / "cowrie.sample.jsonl"


def test_bridge_conversion_schema_valid(tmp_path):
    out = tmp_path / "events.jsonl"
    rc = main(["--in", str(SAMPLE), "--out", str(out),
               "--node", "sensor-a", "--dst-host", "web01"])
    assert rc == 0
    events = load_events(out)
    assert len(events) == 5
    validate_events(events)  # 不抛即合法


def test_bridge_compliance(tmp_path):
    out = tmp_path / "events.jsonl"
    main(["--in", str(SAMPLE), "--out", str(out)])
    raw = out.read_text(encoding="utf-8")
    events = [json.loads(line) for line in raw.splitlines()]

    # /24 截断
    assert all(e["src"]["ip"] == "203.0.113.0/24" for e in events)
    # 密码明文绝不出现；cred_hash 一律 sha256: 前缀
    assert "toor" not in raw and "S3cret!" not in raw
    for e in events:
        if e["who"]["cred_hash"] is not None:
            assert e["who"]["cred_hash"].startswith("sha256:")
    # 事件类型映射顺序
    kinds = [e["what"]["kind"] for e in events]
    assert kinds == ["ssh.connection", "ssh.login_fail", "ssh.login",
                     "ssh.command", "ssh.close"]


def _wait_until(cond, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.2)
    return cond()


def test_follow_mode_survives_and_converts(tmp_path):
    """跟随模式（部署路径）：进程存活、首轮转换 + 追加行持续转换。

    回归背景：旧实现用 `with open(...)` 在首轮读完后关闭句柄，跟随循环
    在已关闭句柄上迭代 → ValueError 崩溃（内网部署实测暴露）。
    """
    src = tmp_path / "cowrie.json"
    src.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "events.jsonl"

    env = dict(os.environ, PYTHONPATH=str(REPO))
    proc = subprocess.Popen(
        [sys.executable, "-m", "entries.cowrie.bridge",
         "--in", str(src), "--out", str(out), "--follow"],
        cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        assert _wait_until(lambda: out.exists() and out.stat().st_size > 0), \
            "跟随模式首轮未转换任何事件"
        assert proc.poll() is None, "跟随模式进程意外退出（句柄关闭 bug 回归）"

        # 追加一行新连接事件，应被持续转换
        first_line = SAMPLE.read_text(encoding="utf-8").splitlines()[0]
        size_before = out.stat().st_size
        with src.open("a", encoding="utf-8") as f:
            f.write(first_line + "\n")
        assert _wait_until(lambda: out.stat().st_size > size_before), \
            "追加行未被跟随转换"
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)
