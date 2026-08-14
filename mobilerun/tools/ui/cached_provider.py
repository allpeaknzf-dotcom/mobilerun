"""PEL Layer 3 — 智能状态提供器（装饰现有 StateProvider，增加缓存层）。

对调用方完全透明：同样有 ``get_state()`` 返回 UIState，并透传 inner
provider 的所有属性（包括 WEB_H5 方案的坐标契约属性）。

核心逻辑（见方案 §7.1）：
1. 计算轻量结构指纹（仅关键元素 + URL/title，不提取全量树）—— 仅 Web 支持
2. 指纹未变 + per-page 命中计数未达上限 → 复用上次 UIState（免截图 & 免树提取）
3. 指纹变化 / 命中达上限 → 完整探测 → 更新指纹、重置命中计数、匹配/发现页面

平台守卫：非 Web 平台没有 ``driver.evaluate``，无法做轻量指纹，因此
每次都走完整探测（行为与未接入 PEL 时完全一致，零退化），但仍会做
页面匹配 + 自动发现，让 page_action 可用。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from mobilerun.tools.ui.state import UIState

logger = logging.getLogger("mobilerun")


class CachedStateProvider:
    """装饰现有 StateProvider，增加智能缓存层。"""

    def __init__(
        self,
        inner_provider,
        cache,
        registry,
        locator_resolver=None,
    ) -> None:
        self._inner = inner_provider
        self._cache = cache
        self._registry = registry
        self._locator_resolver = locator_resolver
        self._platform = str(getattr(inner_provider, "platform", "")).lower() or str(
            getattr(getattr(inner_provider, "driver", None), "platform", "")
        ).lower()
        self._is_web = self._platform == "web"

        self._last_full_state: Optional[UIState] = None
        self._current_page: Optional[dict] = None
        self._last_url: Optional[str] = None
        self._last_title: Optional[str] = None
        # 上次完整探测后算出的「轻量指纹」。注意它与 ElementCache 里的
        # 结构指纹（set_page_fingerprint）算法不同：结构指纹用于坐标缓存的
        # key 命名空间，轻量指纹（URL/title/关键文本/元素数）用于免树提取的
        # 快速比对。两者各司其职，不能混用。
        self._last_lightweight_fp: Optional[str] = None
        self._viewport: dict = {}
        # 最近一次交互可能改变了 DOM / 弹层 / 当前页面判定；置位后下一次
        # get_state() 必须走完整探测，而不是缓存快路径。
        self._force_full_next: bool = False

    # ── 属性透传 ──────────────────────────────────────────────────────

    @property
    def driver(self):
        return self._inner.driver

    @property
    def supported(self) -> set:
        return self._inner.supported

    @property
    def current_page(self) -> Optional[dict]:
        """当前匹配/发现到的页面描述（供 page_action 使用）。"""
        return self._current_page

    def __getattr__(self, name):
        """未显式定义的属性/方法一律透传给 inner provider。

        ``__getattr__`` 仅在常规查找失败时触发，因此本类已定义的
        属性（driver/supported/get_state 等）优先生效。
        """
        # 避免在 __init__ 完成前递归
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    # ── 主入口 ────────────────────────────────────────────────────────

    async def get_state(self) -> UIState:
        if self._force_full_next:
            return await self._full_probe()
        # 非 Web 平台：无 evaluate，无法轻量指纹 → 完整探测（零退化）
        if self._is_web:
            try:
                cached_state = await self._try_cached_state()
                if cached_state is not None:
                    return cached_state
            except Exception as e:  # noqa: BLE001 — 缓存路径异常时安全回退
                logger.debug("Cached fast-path failed, full probe: %s", e)

        return await self._full_probe()

    async def refresh_state(self, force_full: bool = False) -> UIState:
        """供 page_action 混合模式重试链使用的「强制刷新」入口。

        与 ``get_state()`` 的区别在于可显式跳过缓存快速路径：

        - ``force_full=False``：按当前 provider 的默认刷新语义执行（等价于
          ``get_state()``）。Android/iOS 本就没有 Web 那条 ``_try_cached_state()``
          快路径，其 ``get_state()`` 等价于完整刷新，因此原生端用此即可。
        - ``force_full=True``：跳过 ``_try_cached_state()`` 直接 ``_full_probe()``，
          完成完整探测的**全部副作用**（重算指纹、重置命中、重新匹配/发现页面、
          同步 ``current_page`` 与轻量指纹）。Web 重试必须用这个，否则可能只是
          重复读取旧缓存，构成「伪重试」（见方案 §9.3）。

        本方法在类上显式实现，不依赖 ``__getattr__`` 透传，确保后续即使
        ``state_provider`` 被再包一层装饰器，混合模式也不会绑死具体实现。
        """
        if force_full:
            return await self._full_probe()
        return await self.get_state()

    def mark_dirty(self, clear_current_page: bool = True) -> None:
        """标记缓存状态已失效，要求下一次 ``get_state()`` 强制完整探测。

        用于点击、输入、滚动、页面跳转等交互后的「下一帧必须刷新」场景。
        这是框架级失效信号，不依赖某个具体网站是否跳 URL / 改 title。
        """
        self._force_full_next = True
        self._last_lightweight_fp = None
        self._last_full_state = None
        if clear_current_page:
            self._current_page = None
        self._cache.reset_hits()
        self.invalidate_viewport_cache()

    async def _try_cached_state(self) -> Optional[UIState]:
        """尝试走缓存快速路径，命中返回 UIState，否则返回 None。

        判定只看「结构指纹是否一致」+「per-page 命中是否达上限」。指纹一致
        意味着页面结构（元素存在性 + 文本）没变，上次完整探测得到的
        ``_last_full_state``（formatted_text / elements / 截图契约）仍然有效，
        可直接复用 —— 省去本次的 screenshot + DOM 全量提取。

        元素「坐标」缓存是给 page_action 用的独立优化，由 page_action 在定位
        成功时填充，不作为本快速路径的前置条件（否则首次 get_state 永远无坐标
        缓存，会退化为每次完整探测）。
        """
        if self._last_full_state is None:
            return None
        current_fp = await self._compute_lightweight_fingerprint()
        if not current_fp or current_fp != self._last_lightweight_fp:
            return None
        if self._cache.should_force_refresh():
            return None

        self._cache.increment_hit()
        logger.debug(
            "PEL cache hit (page=%s, hits=%d)",
            self._cache.page_fingerprint[:8], self._cache.get_hit_count(),
        )
        return self._last_full_state

    async def _full_probe(self) -> UIState:
        """完整探测 + 更新指纹 + 页面匹配/自动发现。"""
        full_state = await self._inner.get_state()

        new_fp = self._cache.compute_fingerprint(full_state.elements)
        self._cache.set_page_fingerprint(new_fp)
        self._cache.reset_hits()
        self._last_full_state = full_state

        url, title = self._extract_url_title(full_state)
        self._last_url = url
        self._last_title = title
        self._viewport = {
            "width": full_state.screen_width,
            "height": full_state.screen_height,
        }

        # 同步坐标缩放因子到 LocatorResolver
        if self._locator_resolver is not None:
            self._locator_resolver.update_scale(
                getattr(full_state, "coordinate_scale_x", 1.0),
                getattr(full_state, "coordinate_scale_y", 1.0),
            )

        # 页面匹配（Python → YAML → auto）；未命中触发自动发现
        context = self._build_context(full_state)
        matched = self._registry.match(context)
        if matched:
            self._current_page = matched["page"]
            self._cache.set_max_hits(matched["page"].get("max_cached_hits", 5))
        else:
            await self._auto_discover(full_state)

        # 现在 _current_page 已确定（含 key_element_texts），据此算出本页的
        # 轻量指纹快照，供下次 get_state 快速比对。
        self._last_lightweight_fp = await self._compute_lightweight_fingerprint()
        self._force_full_next = False

        return full_state

    async def _auto_discover(self, full_state: UIState) -> None:
        try:
            from mobilerun.auto.discoverer import PageDiscoverer

            discoverer = PageDiscoverer(self._inner.driver, full_state)
            page_def = await discoverer.discover()
            if page_def:
                self._registry.add_auto_page(page_def)
                self._current_page = page_def
            else:
                self._current_page = None
        except Exception as e:  # noqa: BLE001 — 发现失败不影响主流程
            logger.debug("Auto-discovery error: %s", e)
            self._current_page = None

    # ── 轻量指纹（Web 专用）──────────────────────────────────────────

    async def _compute_lightweight_fingerprint(self) -> str:
        """轻量级指纹采集 —— 不提取全量树。失败返回空串（触发完整探测）。"""
        driver = self._inner.driver
        if not hasattr(driver, "evaluate"):
            return ""
        try:
            url = await driver.evaluate("document.location.href")
            title = await driver.evaluate("document.title")
        except Exception:  # noqa: BLE001
            return ""

        title_changed = self._last_title is not None and title != self._last_title
        if title_changed:
            return ""  # title 变了 → 确认页面变化

        from mobilerun.element.resolver import INTERACTIVE_SELECTOR

        key_texts = []
        if self._current_page:
            key_texts = self._current_page.get("key_element_texts", []) or []

        if key_texts:
            check_js = (
                "(() => {"
                "  const results = [];"
                f"  const texts = {json.dumps(key_texts)};"
                f"  const sel = {json.dumps(INTERACTIVE_SELECTOR)};"
                "  const all = document.querySelectorAll(sel);"
                "  texts.forEach(t => {"
                "    for (const el of all) {"
                "      const txt = (el.textContent || '').trim().slice(0,120);"
                "      if (txt.includes(t)) { results.push(t); break; }"
                "    }"
                "  });"
                "  return results.filter(Boolean);"
                "})()"
            )
            try:
                found = await driver.evaluate(check_js)
            except Exception:  # noqa: BLE001
                return ""
            found = found or []
            # 任一 required 关键文本缺失 → 指纹失效
            for t in key_texts:
                if t not in found:
                    return ""
            return hashlib.md5(
                "|".join(list(found) + [url, title]).encode()
            ).hexdigest()

        # 无页面定义 → 交互元素数量 + URL + title 做极简指纹
        count_js = (
            f"document.querySelectorAll({json.dumps(INTERACTIVE_SELECTOR)}).length"
        )
        try:
            count = await driver.evaluate(count_js)
        except Exception:  # noqa: BLE001
            return ""
        return hashlib.md5(f"{url}|{title}|{count}".encode()).hexdigest()

    # ── 上下文构建 ────────────────────────────────────────────────────

    def _build_context(self, ui_state: UIState) -> dict:
        url, title = self._extract_url_title(ui_state)
        elements = []
        for e in self._flatten(ui_state.elements)[:50]:
            elements.append(
                {
                    "text": e.get("text", ""),
                    "tag": e.get("tag", "") or e.get("className", ""),
                }
            )
        return {"url": url, "title": title, "activity": title, "elements": elements}

    @staticmethod
    def _extract_url_title(ui_state: UIState) -> tuple[str, str]:
        """从 UIState 提取 url/title。

        Web/Android provider 把 url 放 phone_state['packageName']、
        title 放 phone_state['currentApp']。也兼容直接挂在 UIState 上的属性。
        """
        ps = getattr(ui_state, "phone_state", {}) or {}
        url = getattr(ui_state, "url", None) or ps.get("packageName", "") or ""
        title = getattr(ui_state, "title", None) or ps.get("currentApp", "") or ""
        return url, title

    @staticmethod
    def _flatten(elements: list[dict]) -> list[dict]:
        result = []
        for el in elements or []:
            result.append(el)
            children = el.get("children")
            if children:
                result.extend(CachedStateProvider._flatten(children))
        return result

    # ── 弹窗联动（供 WEB_H5 方案调用）────────────────────────────────

    def invalidate_viewport_cache(self) -> None:
        """弹窗关闭后失效当前 viewport 缓存坐标。"""
        self._cache.invalidate_viewport(self._viewport)
