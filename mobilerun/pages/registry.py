"""PEL Layer 1 — 多源页面注册表 + 智能匹配。

同时支持三类页面来源，匹配优先级：
  Python 手动页面 > YAML 固化页面 > 运行时自动发现的临时页面

所有来源在内部都规整为统一的 page_desc dict（见 PageObject.to_page_desc），
打分逻辑只认这个统一结构，避免到处分支。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger("mobilerun")


def _score_page_desc(desc: dict, context: dict) -> int:
    """对统一结构的 page_desc 做多维度打分（与 PageObject.match_score 对齐）。"""
    score = 0
    url = context.get("url", "") or ""
    title = context.get("title", "") or ""
    activity = context.get("activity", "") or ""
    element_texts = [
        e.get("text", "") for e in context.get("elements", []) if e.get("text")
    ]

    for pattern in desc.get("url_patterns", []) or []:
        try:
            if pattern and re.search(pattern, url):
                score += 40
                break
        except re.error:
            if pattern and pattern in url:
                score += 40
                break

    for pattern in desc.get("activity_patterns", []) or []:
        try:
            if pattern and re.search(pattern, activity):
                score += 40
                break
        except re.error:
            continue

    for key_text in desc.get("key_element_texts", []) or []:
        if key_text and any(key_text in et for et in element_texts):
            score += 20

    for pattern in desc.get("title_patterns", []) or []:
        try:
            if pattern and re.search(pattern, title):
                score += 20
                break
        except re.error:
            if pattern and pattern in title:
                score += 20
                break

    return min(score, 100)


class PageRegistry:
    """页面注册表 — 多源加载 + 智能匹配。"""

    def __init__(self) -> None:
        # page_id -> page_desc dict（已规整为统一结构）
        self._python_pages: dict[str, dict] = {}
        self._yaml_pages: dict[str, dict] = {}
        self._auto_pages: dict[str, dict] = {}
        self._yaml_dir: Optional[str] = None

    # ── 注册 / 加载 ───────────────────────────────────────────────────

    def register(self, page_cls, platform: str | None = None) -> None:
        """注册手动 Python 页面类（最高优先级）。"""
        from mobilerun.pages.base_page import PageObject

        if isinstance(page_cls, type) and issubclass(page_cls, PageObject):
            instance = page_cls()
        elif isinstance(page_cls, PageObject):
            instance = page_cls
        else:
            raise TypeError(f"register() expects a PageObject subclass, got {page_cls}")
        desc = instance.to_page_desc(platform)
        self._python_pages[desc["page_id"]] = desc

    def load_python_pages(self, platform: str | None = None) -> int:
        """发现并注册内置/用户的 Python PageObject 子类（最高优先级）。

        扫描 ``mobilerun.pages.web`` 与 ``mobilerun.pages.app`` 两个包，把其中
        定义的 PageObject 子类按平台过滤后注册。这条路径让方案里的「全定制
        模式」（手写 PageObject 类）真正可用——否则 PageRegistry 里永远只有
        YAML / auto 页面，手动类不会被加载。

        platform 为 None 时注册全部；否则只注册 ``platform`` 属性匹配的类。
        返回注册数量。
        """
        import importlib
        import pkgutil

        from mobilerun.pages.base_page import PageObject

        plat = platform.lower() if platform else None
        count = 0
        for pkg_name in ("mobilerun.pages.web", "mobilerun.pages.app"):
            try:
                pkg = importlib.import_module(pkg_name)
            except Exception as e:  # noqa: BLE001
                logger.debug("Cannot import page package %s: %s", pkg_name, e)
                continue
            for _, modname, _ in pkgutil.iter_modules(pkg.__path__):
                try:
                    mod = importlib.import_module(f"{pkg_name}.{modname}")
                except Exception as e:  # noqa: BLE001
                    logger.warning("Skipping page module %s: %s", modname, e)
                    continue
                for obj in vars(mod).values():
                    if (
                        isinstance(obj, type)
                        and issubclass(obj, PageObject)
                        and obj is not PageObject
                    ):
                        obj_plat = str(getattr(obj, "platform", "web")).lower()
                        if plat and obj_plat != plat:
                            continue
                        self.register(obj)
                        count += 1
        if count:
            logger.debug("Registered %d Python PageObject(s)", count)
        return count

    def load_yaml_pages(self, yaml_dir: str = ".mobilerun/pages/") -> int:
        """加载自动发现 + 用户微调的 YAML 页面描述（中等优先级）。

        返回成功加载的页面数。目录不存在时静默返回 0。
        """
        import yaml

        self._yaml_dir = yaml_dir
        if not os.path.isdir(yaml_dir):
            return 0

        count = 0
        for fname in sorted(os.listdir(yaml_dir)):
            if not (fname.endswith(".yaml") or fname.endswith(".yml")):
                continue
            path = os.path.join(yaml_dir, fname)
            try:
                with open(path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
            except Exception as e:  # noqa: BLE001 — 用户编辑可能造成格式错误
                logger.warning("Skipping malformed page YAML %s: %s", path, e)
                continue
            if not isinstance(data, dict):
                continue
            desc = self._normalize_yaml_desc(data)
            page_id = desc.get("page_id") or os.path.splitext(fname)[0]
            desc["page_id"] = page_id
            self._yaml_pages[page_id] = desc
            count += 1
        logger.debug("Loaded %d YAML page descriptions from %s", count, yaml_dir)
        return count

    @staticmethod
    def _normalize_yaml_desc(data: dict) -> dict:
        """补齐 YAML 中可能缺失的字段，保证统一结构。"""
        data.setdefault("url_patterns", [])
        data.setdefault("title_patterns", [])
        data.setdefault("key_element_texts", [])
        data.setdefault("activity_patterns", [])
        data.setdefault("match_threshold", 60)
        data.setdefault("max_cached_hits", 5)
        data.setdefault("elements", {})
        return data

    def add_auto_page(self, page_desc: dict) -> None:
        """运行时添加自动发现的页面描述（优先级最低）。

        如果同名 YAML 固化页面已存在（用户编辑过），跳过注入。
        """
        page_id = page_desc.get("page_id", "")
        if page_id and page_id not in self._yaml_pages:
            self._normalize_yaml_desc(page_desc)
            self._auto_pages[page_id] = page_desc

    # ── 匹配 ──────────────────────────────────────────────────────────

    def match(self, context: dict) -> Optional[dict]:
        """多源匹配当前页面。

        返回 {"source", "page_id", "page": page_desc, "score"}，未命中返回 None。
        匹配流程：Python → YAML → auto，先命中阈值者胜。
        """
        for source, pages in (
            ("python", self._python_pages),
            ("yaml", self._yaml_pages),
            ("auto", self._auto_pages),
        ):
            best = None
            best_score = -1
            for page_id, desc in pages.items():
                threshold = desc.get("match_threshold", 60)
                score = _score_page_desc(desc, context)
                if score >= threshold and score > best_score:
                    best = (page_id, desc)
                    best_score = score
            if best is not None:
                return {
                    "source": source,
                    "page_id": best[0],
                    "page": best[1],
                    "score": best_score,
                }
        return None

    # ── 元素定位策略提取 ──────────────────────────────────────────────

    @staticmethod
    def get_element_locators(
        page: dict, element_name: str, platform: str = "web"
    ) -> list[dict]:
        """从页面描述中提取元素的定位策略列表（短格式，交给 resolver 归一化）。"""
        if not page:
            return []
        el = page.get("elements", {}).get(element_name)
        if not el:
            return []
        return list(el.get("detected_selectors", []))

    # ── 调试辅助 ──────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "python": len(self._python_pages),
            "yaml": len(self._yaml_pages),
            "auto": len(self._auto_pages),
        }
