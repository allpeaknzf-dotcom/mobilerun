"""PEL Layer 1 — PageObject 基类与元素描述（手动定义，可选增强）。

没有 PageObject 定义时，系统通过自动发现引擎工作（首次慢、重复快）。
有定义时，跳过自动发现，使用用户指定的高精度选择器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ElementSpec:
    """页面元素的跨平台定位描述。"""

    name: str
    # locators 按平台分: {"web": [...], "android": [...], "ios": [...]}
    # 通用策略放 "common" key，被所有平台继承。
    # 每个 locator 用短格式 dict，如 {"css": "#btn"} / {"text": "登录"}。
    locators: dict[str, list[dict]] = field(default_factory=dict)
    cache_ttl: float = 30.0
    post_action_wait: float = 0.5
    required: bool = True
    # 操作该元素后应失效缓存的元素名列表（AJAX 局部刷新补偿）。
    # page_action 未显式传 invalidate_after 时，回退到这里声明的值。
    invalidate_after: list[str] = field(default_factory=list)


class PageObject:
    """页面对象基类 — 子类声明匹配规则 + elements。

    页面匹配采用多维度打分（总分 >= match_threshold 则匹配）：
    - url_patterns (Web): 正则列表，命中 +40
    - key_element_texts: 关键文本，每命中一个 +20
    - title_patterns: 页面标题正则，命中 +20
    """

    url_patterns: list[str] = []
    activity_patterns: list[str] = []
    screen_patterns: list[str] = []
    key_element_texts: list[str] = []
    title_patterns: list[str] = []

    platform: str = "web"
    match_threshold: int = 60
    max_cached_hits: int = 5

    elements: dict[str, ElementSpec] = {}

    # ── 定位器 ────────────────────────────────────────────────────────

    def get_locators(self, name: str, platform: str = "web") -> list[dict]:
        """获取指定元素在当前平台的定位策略列表（platform-specific + common）。"""
        spec = self.elements.get(name)
        if not spec:
            return []
        common = spec.locators.get("common", [])
        platform_specific = spec.locators.get(platform, [])
        return list(platform_specific) + list(common)

    def fingerprint_elements(self) -> list[str]:
        """返回作为页面指纹的关键元素名（required=True）。"""
        return [name for name, spec in self.elements.items() if spec.required]

    # ── 匹配打分 ──────────────────────────────────────────────────────

    def match_score(self, context: dict) -> int:
        """多维度页面匹配打分，返回 0-100。

        context: {"url", "title", "activity", "elements": [{"text", "tag"}, ...]}
        """
        score = 0
        url = context.get("url", "") or ""
        title = context.get("title", "") or ""
        activity = context.get("activity", "") or ""
        element_texts = [
            e.get("text", "") for e in context.get("elements", []) if e.get("text")
        ]

        for pattern in self.url_patterns:
            if pattern and re.search(pattern, url):
                score += 40
                break

        for pattern in self.activity_patterns:
            if pattern and re.search(pattern, activity):
                score += 40
                break

        for key_text in self.key_element_texts:
            if key_text and any(key_text in et for et in element_texts):
                score += 20

        for pattern in self.title_patterns:
            if pattern and re.search(pattern, title):
                score += 20
                break

        return min(score, 100)

    # ── 序列化为统一 dict（与 YAML / auto 页面描述同构）─────────────

    def to_page_desc(self, platform: str | None = None) -> dict:
        """转成 PageRegistry 内部统一使用的 page_desc dict 结构。

        统一结构::

            {
              "page_id", "platform", "match_threshold", "max_cached_hits",
              "url_patterns", "title_patterns", "key_element_texts",
              "elements": {name: {"detected_selectors": [...], "required": bool,
                                  "post_action_wait": float}},
            }
        """
        plat = platform or self.platform
        elements_out: dict[str, dict] = {}
        for name, spec in self.elements.items():
            selectors = list(spec.locators.get(plat, [])) + list(
                spec.locators.get("common", [])
            )
            elements_out[name] = {
                "detected_selectors": selectors,
                "required": spec.required,
                "post_action_wait": spec.post_action_wait,
                "cache_ttl": spec.cache_ttl,
                "invalidate_after": list(spec.invalidate_after),
            }
        return {
            "page_id": self.__class__.__name__,
            "platform": plat,
            "match_threshold": self.match_threshold,
            "max_cached_hits": self.max_cached_hits,
            "url_patterns": list(self.url_patterns),
            "title_patterns": list(self.title_patterns),
            "key_element_texts": list(self.key_element_texts),
            "activity_patterns": list(self.activity_patterns),
            "elements": elements_out,
        }
