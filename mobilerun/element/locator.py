"""定位策略定义 + 选择器归一化。

本模块只做纯数据结构和格式转换，不依赖 driver/provider，方便单测。

选择器在系统里有两种来源、两种写法：

1. YAML / 自动发现的「短格式」：``{"css": "#btn"}``、``{"text": "登录"}``、
   ``{"spatial": "below:用户名"}``、``{"id": "com.x:id/btn"}``、
   ``{"placeholder": "用户名"}``、``{"xpath": "//div"}``
2. 代码内部的「规范格式」：``{"type": Strategy.CSS_SELECTOR, "value": "#btn"}``

``LocatorResolver`` 统一消费规范格式，因此所有入口先经过
``normalize_selectors()`` 转换。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Optional


class Strategy(IntEnum):
    """定位策略优先级（值越小越优先）。"""

    CSS_SELECTOR = 1
    ACCESSIBILITY_ID = 2
    TEXT_EXACT = 3
    TEXT_FUZZY = 4
    PLACEHOLDER = 5
    SPATIAL = 6
    VISION_FALLBACK = 7
    DOM_DEEP_SCAN = 8


@dataclass
class LocatorResult:
    """定位结果。"""

    success: bool
    x: int = 0
    y: int = 0
    strategy_used: Strategy = Strategy.VISION_FALLBACK
    cache_hit: bool = False
    element_index: Optional[int] = None
    element_info: Optional[dict] = None
    error: str = ""
    diagnostics: Optional[dict] = None


@dataclass
class LocatorStrategy:
    """单个定位策略（规范格式的 dataclass 形态）。"""

    type: Strategy
    value: str
    extra: Optional[dict] = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"type": self.type, "value": self.value}
        if self.extra:
            d.update(self.extra)
        return d


# 短格式 key → Strategy 的映射。注意 "text" 默认按 fuzzy 处理，
# 需要精确匹配时短格式应写 {"text_exact": "..."}。
_SHORT_KEY_TO_STRATEGY = {
    "css": Strategy.CSS_SELECTOR,
    "xpath": Strategy.CSS_SELECTOR,  # xpath 走同一 CSS 通道（evaluate 内分辨）
    "id": Strategy.ACCESSIBILITY_ID,
    "accessibility_id": Strategy.ACCESSIBILITY_ID,
    "resource_id": Strategy.ACCESSIBILITY_ID,
    "text": Strategy.TEXT_FUZZY,
    "text_exact": Strategy.TEXT_EXACT,
    "text_fuzzy": Strategy.TEXT_FUZZY,
    "placeholder": Strategy.PLACEHOLDER,
    "spatial": Strategy.SPATIAL,
    "vision": Strategy.VISION_FALLBACK,
}


def _coerce_type(raw: Any) -> Optional[Strategy]:
    """把任意类型（Strategy / int / str 名称）转成 Strategy。"""
    if isinstance(raw, Strategy):
        return raw
    if isinstance(raw, int):
        try:
            return Strategy(raw)
        except ValueError:
            return None
    if isinstance(raw, str):
        name = raw.strip().upper()
        if name in Strategy.__members__:
            return Strategy[name]
        # 兼容短 key 名（如 "css"）
        low = raw.strip().lower()
        return _SHORT_KEY_TO_STRATEGY.get(low)
    return None


def normalize_selector(sel: dict) -> Optional[dict]:
    """把单个选择器（短格式或规范格式）归一化为规范格式 dict。

    规范格式：``{"type": Strategy, "value": str, ...其它原始 key 透传}``
    无法识别时返回 None。
    """
    if not isinstance(sel, dict):
        return None

    # 已是规范格式
    if "type" in sel:
        stype = _coerce_type(sel["type"])
        if stype is None:
            return None
        out = dict(sel)
        out["type"] = stype
        out.setdefault("value", sel.get("value", ""))
        return out

    # 短格式：取第一个识别到的 key
    for key, value in sel.items():
        stype = _SHORT_KEY_TO_STRATEGY.get(key.lower())
        if stype is not None:
            out = {"type": stype, "value": value}
            # 透传可能存在的额外参数（如 index/ttl）
            for extra_key in ("index", "ttl"):
                if extra_key in sel:
                    out[extra_key] = sel[extra_key]
            return out
    return None


def normalize_selectors(selectors: list[dict] | None) -> list[dict]:
    """批量归一化，丢弃无法识别的项，保留原有顺序（即优先级）。"""
    if not selectors:
        return []
    result = []
    for sel in selectors:
        norm = normalize_selector(sel)
        if norm is not None:
            result.append(norm)
    return result
