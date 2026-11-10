"""规范序列化与「逐字节一致」判定口径（契约 12 §6.3）。

口径：
1. JSON 排序键、紧凑分隔符、UTF-8、无 BOM、\\n 行尾；
2. 输出中禁止浮点数（数值一律 int 或 str）——违反即抛错；
3. 时间不来自系统时钟，随机不来自未播种源（K5）。
"""
from __future__ import annotations

import hashlib
import json


def dumps_canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def assert_no_floats(obj, path: str = "$") -> None:
    if isinstance(obj, float):
        raise ValueError(f"输出中出现浮点数 {path}={obj!r}（违反逐字节一致口径 12 §6.3）")
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert_no_floats(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            assert_no_floats(v, f"{path}[{i}]")


def write_jsonl(path, records) -> str:
    """写 JSONL（每行 canonical），返回整文件 SHA-256。"""
    buf = bytearray()
    for r in records:
        assert_no_floats(r)
        buf += dumps_canonical(r) + b"\n"
    path.write_bytes(bytes(buf))
    return hashlib.sha256(bytes(buf)).hexdigest()


def write_json(path, obj) -> str:
    """写单个 JSON（canonical），返回 SHA-256。"""
    assert_no_floats(obj)
    b = dumps_canonical(obj)
    path.write_bytes(b)
    return hashlib.sha256(b).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
