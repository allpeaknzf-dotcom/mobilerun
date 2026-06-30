"""PEL Layer 2 — viewport 感知的结构指纹缓存。

设计要点（见方案 §6.4）：
- cache_key = viewport_hash : page_fingerprint : element_name
- 坐标始终存「原始设备像素」，取时由 LocatorResolver 按当前 scale 换算
- 页面指纹变化 → 旧指纹的缓存条目全量失效（内存泄漏防护）
- 连续命中计数器 per-page（key = page_fingerprint），非全局
- 超过 max_entries 触发 LRU 淘汰
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class CacheEntry:
    """单个元素的缓存条目。坐标为原始设备像素。"""

    x: int
    y: int
    strategy: str
    cached_at: float
    ttl: float
    viewport_hash: str = ""

    def is_expired(self) -> bool:
        return time.time() - self.cached_at > self.ttl


class ElementCache:
    """元素坐标缓存。"""

    def __init__(
        self,
        default_ttl: float = 30.0,
        max_cached_hits: int = 5,
        max_entries: int = 500,
    ) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._page_fingerprint: str = ""
        self.default_ttl = default_ttl
        self.max_cached_hits = max_cached_hits
        self.max_entries = max_entries
        # per-page 连续命中计数器: {page_fingerprint: hit_count}
        self._consecutive_hits: dict[str, int] = {}
        # LRU 淘汰队列: 最近使用在末尾
        self._lru_order: list[str] = []

    # ── key 计算 ──────────────────────────────────────────────────────

    def _viewport_hash(self, viewport: Optional[dict]) -> str:
        if not viewport:
            return "default"
        return hashlib.md5(
            f"{viewport.get('width', 0)}x{viewport.get('height', 0)}".encode()
        ).hexdigest()[:8]

    def _cache_key(self, element_name: str, viewport: Optional[dict]) -> str:
        vp = self._viewport_hash(viewport)
        return f"{vp}:{self._page_fingerprint}:{element_name}"

    # ── 页面指纹 / 命中计数 ───────────────────────────────────────────

    @property
    def page_fingerprint(self) -> str:
        return self._page_fingerprint

    def set_page_fingerprint(self, fp: str) -> None:
        """页面指纹变化 → 清空旧缓存条目 + 重置旧指纹命中计数。"""
        if fp == self._page_fingerprint:
            return
        old_fp = self._page_fingerprint
        self._page_fingerprint = fp
        if old_fp:
            # 清理旧指纹的所有缓存条目
            stale = [k for k in self._entries if f":{old_fp}:" in k]
            for k in stale:
                del self._entries[k]
                if k in self._lru_order:
                    self._lru_order.remove(k)
            self._consecutive_hits.pop(old_fp, None)

    def get_hit_count(self) -> int:
        return self._consecutive_hits.get(self._page_fingerprint, 0)

    def increment_hit(self) -> None:
        fp = self._page_fingerprint
        self._consecutive_hits[fp] = self._consecutive_hits.get(fp, 0) + 1

    def reset_hits(self) -> None:
        self._consecutive_hits[self._page_fingerprint] = 0

    def should_force_refresh(self) -> bool:
        return self.get_hit_count() >= self.max_cached_hits

    def set_max_hits(self, n: int) -> None:
        """允许页面定义覆盖单页面的缓存命中上限。"""
        if n and n > 0:
            self.max_cached_hits = n

    # ── 读写 ──────────────────────────────────────────────────────────

    def get(
        self, element_name: str, viewport: Optional[dict] = None
    ) -> Optional[CacheEntry]:
        key = self._cache_key(element_name, viewport)
        entry = self._entries.get(key)
        if entry and not entry.is_expired():
            if key in self._lru_order:
                self._lru_order.remove(key)
            self._lru_order.append(key)
            return entry
        if entry:
            # 过期清理
            del self._entries[key]
            if key in self._lru_order:
                self._lru_order.remove(key)
        return None

    def set(
        self,
        element_name: str,
        x: int,
        y: int,
        strategy: str,
        ttl: Optional[float] = None,
        viewport: Optional[dict] = None,
    ) -> None:
        key = self._cache_key(element_name, viewport)
        if key not in self._entries and len(self._entries) >= self.max_entries:
            # LRU 淘汰最久未使用
            if self._lru_order:
                evicted = self._lru_order.pop(0)
                self._entries.pop(evicted, None)
        self._entries[key] = CacheEntry(
            x=x,
            y=y,
            strategy=strategy,
            cached_at=time.time(),
            ttl=ttl if ttl is not None else self.default_ttl,
            viewport_hash=self._viewport_hash(viewport),
        )
        if key in self._lru_order:
            self._lru_order.remove(key)
        self._lru_order.append(key)

    def invalidate_viewport(self, viewport: Optional[dict]) -> None:
        """弹窗关闭后失效该 viewport 内所有缓存坐标（布局可能变化）。"""
        vp_hash = self._viewport_hash(viewport)
        keys_to_del = [k for k in self._entries if k.startswith(f"{vp_hash}:")]
        for k in keys_to_del:
            del self._entries[k]
            if k in self._lru_order:
                self._lru_order.remove(k)

    def invalidate_element(
        self, element_name: str, viewport: Optional[dict] = None
    ) -> bool:
        """失效单个元素的缓存坐标（AJAX 局部刷新补偿）。

        返回是否确实删除了一条缓存。供 page_action 的 invalidate_after 使用，
        避免调用方触碰内部结构。
        """
        key = self._cache_key(element_name, viewport)
        existed = key in self._entries
        self._entries.pop(key, None)
        if key in self._lru_order:
            self._lru_order.remove(key)
        return existed

    def clear(self) -> None:
        self._entries.clear()
        self._lru_order.clear()
        self._consecutive_hits.clear()
        self._page_fingerprint = ""

    # ── 结构指纹 ──────────────────────────────────────────────────────

    def compute_fingerprint(self, elements: list[dict]) -> str:
        """结构指纹 = MD5(排序后的「tag/className : type : text[:40]」)。

        只基于元素的存在性和文本，不依赖坐标，因此页面元素因动画/滚动
        移动位置时指纹不变。Web 元素用 className，原生元素用 tag。
        """
        structural = []
        for el in self._flatten(elements):
            tag = el.get("tag", "") or el.get("className", "")
            typ = el.get("type", "")
            text = (el.get("text", "") or "")[:40]
            if not (tag or text):
                continue
            structural.append(f"{tag}:{typ}:{text}")
        structural.sort()
        return hashlib.md5("|".join(structural).encode()).hexdigest()

    @staticmethod
    def _flatten(elements: list[dict]) -> list[dict]:
        result = []
        for el in elements or []:
            result.append(el)
            children = el.get("children")
            if children:
                result.extend(ElementCache._flatten(children))
        return result
