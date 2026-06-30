"""PEL Layer 0 — 自动发现引擎。

首次访问未知页面时，基于「已经获取到的 UIState」（不额外探测）：
1. 从 UIState.elements 整理交互元素
2. 为每个元素检测最优选择器
   - Web：注入 JS 读取 id/name/data-testid/aria/placeholder/text
   - Android/iOS：直接用元素已有的 resourceId / text 字段
3. 生成 .mobilerun/pages/auto_<page_id>.yaml 并返回 page_desc

用户零操作即可享受后续访问的缓存加速。
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from mobilerun.auto.yaml_generator import YAMLGenerator

logger = logging.getLogger("mobilerun")

# 为每个交互元素检测选择器的 JS（按稳定性排序）。
DETECT_SELECTORS_JS = r"""
(interactiveSelector, index) => {
  const all = document.querySelectorAll(interactiveSelector);
  const el = all[index];
  const results = [];
  if (!el) return results;
  if (el.id) results.push({css: '#' + CSS.escape(el.id)});
  if (el.name) results.push({css: el.tagName.toLowerCase() + '[name="' + el.name + '"]'});
  if (el.dataset && el.dataset.testid) results.push({css: '[data-testid="' + el.dataset.testid + '"]'});
  const aria = el.getAttribute('aria-label');
  if (aria) results.push({text: aria.trim().slice(0, 40)});
  if (el.placeholder) results.push({css: el.tagName.toLowerCase() + '[placeholder*="' + el.placeholder.slice(0, 30) + '"]'});
  const text = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
  if (text && text.length >= 2) results.push({text: text});
  return results;
}
"""


class PageDiscoverer:
    """自动发现当前页面元素并生成页面描述。"""

    def __init__(
        self,
        driver,
        ui_state,
        yaml_dir: str = ".mobilerun/pages/",
        max_elements: int = 60,
    ) -> None:
        self.driver = driver
        self.ui_state = ui_state
        self.platform = str(getattr(driver, "platform", "web")).lower()
        self.generator = YAMLGenerator(yaml_dir)
        self.max_elements = max_elements

    async def discover(self) -> Optional[dict]:
        """基于已有 UIState 发现页面，写 YAML，返回 page_desc dict。"""
        try:
            url = self._get_url()
            title = self._get_title()
            interactive = self._collect_interactive()
            if not interactive:
                return None

            elements: dict[str, dict] = {}
            used_names: set[str] = set()
            from mobilerun.element.resolver import INTERACTIVE_SELECTOR

            for el in interactive:
                name = self._element_name(el, used_names)
                used_names.add(name)
                selectors = await self._detect_selectors(el, INTERACTIVE_SELECTOR)
                elements[name] = {
                    "detected_selectors": selectors,
                    "last_bounds": el.get("bounds", ""),
                    "last_seen": self._now(),
                    "required": el.get("_required", False),
                }

            base_id = self._generate_page_id(url, title)
            page_id = f"auto_{base_id}"
            url_patterns = [self._extract_domain_path(url)] if url else []
            key_texts = [
                (el.get("text", "") or "")[:30]
                for el in interactive[:5]
                if el.get("text")
            ]

            yaml_data = self.generator.build_yaml_data(
                page_id=page_id,
                url_patterns=url_patterns,
                title_patterns=[title] if title else [],
                key_element_texts=key_texts,
                platform=self.platform,
                viewport=self._viewport_str(),
                elements=elements,
                generated_at=self._now(),
            )
            yaml_path = self.generator.write(page_id, yaml_data)

            # 返回的 page_desc 与 PageRegistry 统一结构对齐
            page_desc = dict(yaml_data)
            page_desc["fingerprint"] = self._compute_fingerprint(interactive)
            page_desc["yaml_path"] = yaml_path
            logger.debug("Auto-discovered page '%s' (%d elements)", page_id, len(elements))
            return page_desc
        except Exception as e:  # noqa: BLE001 — 自动发现失败不能影响主流程
            logger.debug("Auto-discovery failed: %s", e)
            return None

    # ── 元素采集 ──────────────────────────────────────────────────────

    def _collect_interactive(self) -> list[dict]:
        """从 UIState.elements 整理出交互元素（扁平化，限量）。"""
        flat = self._flatten(self.ui_state.elements)
        out = []
        for el in flat:
            # 必须有可点击坐标（bounds），跳过纯容器
            if not (el.get("bounds") or el.get("boundsInScreen")):
                continue
            if el.get("index") is None and not el.get("text"):
                continue
            out.append(el)
            if len(out) >= self.max_elements:
                break
        return out

    async def _detect_selectors(
        self, element: dict, interactive_selector: str
    ) -> list[dict]:
        """检测元素选择器。Web 走 JS，原生用现有字段。"""
        if self.platform == "web" and hasattr(self.driver, "evaluate"):
            index = element.get("index")
            if index is None:
                return self._fallback_selectors(element)
            import json

            try:
                result = await self.driver.evaluate(
                    f"({DETECT_SELECTORS_JS})("
                    f"{json.dumps(interactive_selector)}, {int(index)})"
                )
                if result:
                    return result
            except Exception as e:  # noqa: BLE001
                logger.debug("Selector detection JS failed: %s", e)
            return self._fallback_selectors(element)
        return self._native_selectors(element)

    @staticmethod
    def _fallback_selectors(element: dict) -> list[dict]:
        text = (element.get("text", "") or "").strip()
        # 去掉 normalize 追加的 " [href, ...]" 后缀
        text = re.sub(r"\s*\[[^\]]*\]\s*$", "", text).strip()
        sels = []
        if text and len(text) >= 2:
            sels.append({"text": text[:40]})
        return sels

    def _native_selectors(self, element: dict) -> list[dict]:
        """Android/iOS：用 resourceId / text 生成选择器。"""
        sels = []
        rid = element.get("resourceId", "")
        if rid:
            sels.append({"id": rid})
        text = (element.get("text", "") or "").strip()
        if text and len(text) >= 1:
            sels.append({"text": text[:40]})
        cd = element.get("contentDescription", "")
        if cd:
            sels.append({"text": cd[:40]})
        return sels

    # ── 命名 / ID 生成 ────────────────────────────────────────────────

    def _element_name(self, element: dict, used: set[str]) -> str:
        text = (element.get("text", "") or "").strip()
        text = re.sub(r"\s*\[[^\]]*\]\s*$", "", text).strip()
        base = text[:30] if text else ""
        if not base:
            cls = element.get("className", "") or element.get("tag", "")
            base = f"{cls or 'element'}_{element.get('index', 0)}"
        name = base
        suffix = 1
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        return name

    def _generate_page_id(self, url: str, title: str) -> str:
        if url:
            parsed = urlparse(url if "://" in url else f"http://{url}")
            host = (parsed.netloc or "").replace(".", "_")
            path = (parsed.path or "").strip("/").replace("/", "_")
            base = f"{host}_{path}".strip("_") or host or "page"
        elif title:
            base = re.sub(r"\W+", "_", title).strip("_")
        else:
            base = "page"
        base = re.sub(r"[^0-9A-Za-z_一-鿿]", "_", base)[:50].strip("_")
        # 加短 hash 避免不同 query 的同 path 撞名过度
        digest = hashlib.md5(f"{url}|{title}".encode()).hexdigest()[:6]
        return f"{base}_{digest}" if base else f"page_{digest}"

    @staticmethod
    def _extract_domain_path(url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(url if "://" in url else f"http://{url}")
        return f"{parsed.netloc}{parsed.path}".rstrip("/")

    def _compute_fingerprint(self, elements: list[dict]) -> str:
        structural = []
        for el in elements:
            tag = el.get("tag", "") or el.get("className", "")
            typ = el.get("type", "")
            text = (el.get("text", "") or "")[:40]
            structural.append(f"{tag}:{typ}:{text}")
        structural.sort()
        return hashlib.md5("|".join(structural).encode()).hexdigest()

    # ── 上下文读取 ────────────────────────────────────────────────────

    def _get_url(self) -> str:
        ps = getattr(self.ui_state, "phone_state", {}) or {}
        return ps.get("packageName", "") or getattr(self.ui_state, "url", "") or ""

    def _get_title(self) -> str:
        ps = getattr(self.ui_state, "phone_state", {}) or {}
        return ps.get("currentApp", "") or getattr(self.ui_state, "title", "") or ""

    def _viewport_str(self) -> str:
        vp = getattr(self.driver, "_viewport", None)
        if vp:
            return f"{vp.get('width', 0)}x{vp.get('height', 0)}"
        w = getattr(self.ui_state, "screen_width", 0)
        h = getattr(self.ui_state, "screen_height", 0)
        return f"{w}x{h}"

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _flatten(elements: list[dict]) -> list[dict]:
        result = []
        for el in elements or []:
            result.append(el)
            children = el.get("children")
            if children:
                result.extend(PageDiscoverer._flatten(children))
        return result
