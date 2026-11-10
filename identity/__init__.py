"""身份解析（契约 12 §2.3/§2.3a）：M0 最小视图（单事件指纹）+ M1 多视图合并。"""
from __future__ import annotations

from .multiview import MultiViewResolver
from .resolver import resolve

__all__ = ["resolve", "MultiViewResolver"]
