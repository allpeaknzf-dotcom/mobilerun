"""PEL Layer 2 — 元素定位引擎（平台感知的多策略定位 + viewport 感知缓存）。"""

from mobilerun.element.cache import CacheEntry, ElementCache
from mobilerun.element.locator import (
    LocatorResult,
    LocatorStrategy,
    Strategy,
    normalize_selectors,
)
from mobilerun.element.resolver import LocatorResolver

__all__ = [
    "CacheEntry",
    "ElementCache",
    "LocatorResult",
    "LocatorStrategy",
    "Strategy",
    "normalize_selectors",
    "LocatorResolver",
]
