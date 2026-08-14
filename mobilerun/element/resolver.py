"""PEL Layer 2 — 平台感知的定位执行器。

关键设计决策（见方案 §6.3）：Web 平台不复用 ``element_search.py``，
因为该模块依赖 Android 专有字段（contentDescription/hint/resourceId/
isClickable），而 Web ``normalize_element()`` 产出 className/text/
boundsInScreen/checkedState。Web 在本模块内实现独立的 text/spatial 匹配；
Android/iOS 仍复用 ``element_search.Filters``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from mobilerun.element.cache import ElementCache
from mobilerun.element.locator import LocatorResult, Strategy, normalize_selectors

logger = logging.getLogger("mobilerun")

# 平台 → 可用策略映射（自动跳过不适用的策略，避免无效尝试）
PLATFORM_STRATEGIES: dict[str, list[Strategy]] = {
    "web": [
        Strategy.CSS_SELECTOR,
        Strategy.TEXT_EXACT,
        Strategy.TEXT_FUZZY,
        Strategy.PLACEHOLDER,
        Strategy.SPATIAL,
        Strategy.VISION_FALLBACK,
    ],
    "android": [
        Strategy.ACCESSIBILITY_ID,
        Strategy.TEXT_EXACT,
        Strategy.TEXT_FUZZY,
        Strategy.SPATIAL,
        Strategy.VISION_FALLBACK,
    ],
    "ios": [
        Strategy.ACCESSIBILITY_ID,
        Strategy.TEXT_EXACT,
        Strategy.TEXT_FUZZY,
        Strategy.SPATIAL,
        Strategy.VISION_FALLBACK,
    ],
}

# 交互元素选择器 —— 与 dom_extractor.DOM_EXTRACTOR_JS 保持一致。
INTERACTIVE_SELECTOR = (
    'a[href],button,input:not([type="hidden"]),select,textarea,'
    '[role="button"],[role="link"],[role="textbox"],[role="searchbox"],'
    '[role="combobox"],[role="listbox"],[role="menuitem"],[role="tab"],'
    '[role="switch"],[role="checkbox"],[role="radio"],'
    '[onclick],[tabindex]:not([tabindex="-1"]),summary,details,label,legend,'
    'div[class*="btn"],div[class*="button"],div[class*="confirm"],div[class*="operate"],div[class*="action"],'
    'span[class*="btn"],span[class*="confirm"],span[class*="cancel"],span[class*="operate"],span[class*="action"]'
)

# CSS 定位 JS：注入浏览器，返回元素中心点（视口坐标）。
CSS_QUERY_JS = r"""
(element, interactiveSelector) => {
  const css = element.css;
  const idx = element.index;
  let el = null;
  if (idx !== undefined && idx !== null) {
    const all = document.querySelectorAll(interactiveSelector);
    el = all[idx];
  } else if (css && css.startsWith('xpath=')) {
    const xp = css.slice(6);
    const r = document.evaluate(xp, document, null,
      XPathResult.FIRST_ORDERED_NODE_TYPE, null);
    el = r.singleNodeValue;
  } else if (css) {
    try { el = document.querySelector(css); } catch (e) { el = null; }
  }
  if (!el) return null;
  const rect = el.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return null;
  const style = window.getComputedStyle(el);
  if (style.visibility === 'hidden' || style.display === 'none') return null;
  if (parseFloat(style.opacity) === 0) return null;
  return {
    x: Math.round(rect.left + rect.width / 2),
    y: Math.round(rect.top + rect.height / 2),
    bounds: Math.round(rect.left) + ',' + Math.round(rect.top) + ','
          + Math.round(rect.right) + ',' + Math.round(rect.bottom),
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type') || el.getAttribute('role') || '',
    text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120),
  };
}
"""


# ── DOM 深度扫描（Phase B，仅 Web/H5）────────────────────────────────────
#
# 设计依据：方案 §10。与 DOM_EXTRACTOR_JS 共用 INTERACTIVE_SELECTOR，但**不**
# 复用其返回结构 —— 深度扫描追求「字段更丰富、可打分、可诊断」，目标不同
# （见方案 §10.5）。扫描范围：顶层 document + open shadow root，不自动深入
# 跨域 iframe（同源 iframe 本轮也不进，避免实现复杂度过高，见 §10.4）。
DEEP_DOM_SCAN_JS = r"""
(interactiveSelector, maxCandidates) => {
  const out = [];
  let iframeDetected = false;
  let crossOriginIframe = false;
  let shadowRootsScanned = 0;
  const squashText = (value) => (value || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const extractLabel = (el) => {
    const attrLabel = (el.getAttribute && (
      el.getAttribute('aria-label')
      || el.getAttribute('placeholder')
      || el.getAttribute('title')
      || el.getAttribute('alt')
    )) || '';
    if (attrLabel) return squashText(attrLabel);
    let cleaned = '';
    try {
      const clone = el.cloneNode(true);
      clone.querySelectorAll(
        '[role="menu"],[role="listbox"],[role="tree"],[role="dialog"],script,style'
      ).forEach((node) => node.remove());
      cleaned = squashText(clone.innerText || clone.textContent || '');
    } catch (e) {}
    if (cleaned) return cleaned;
    return squashText(el.innerText || el.textContent || '');
  };

  const collectFrom = (root, source, depth) => {
    let nodes = [];
    try { nodes = Array.from(root.querySelectorAll(interactiveSelector)); }
    catch (e) { nodes = []; }
    for (const el of nodes) {
      let rect, style;
      try {
        rect = el.getBoundingClientRect();
        style = window.getComputedStyle(el);
      } catch (e) { continue; }
      const visible = !!(style && style.visibility !== 'hidden'
        && style.display !== 'none' && parseFloat(style.opacity || '1') !== 0
        && rect.width > 0 && rect.height > 0);
      const vw = window.innerWidth || document.documentElement.clientWidth || 0;
      const vh = window.innerHeight || document.documentElement.clientHeight || 0;
      const inViewport = !!(rect.bottom > 0 && rect.right > 0
        && rect.top < vh && rect.left < vw);
      const tag = (el.tagName || '').toLowerCase();
      const aria = el.getAttribute && (el.getAttribute('aria-label') || '');
      const role = (el.getAttribute && el.getAttribute('role')) || '';
      const type = (el.getAttribute && el.getAttribute('type')) || '';
      const isDisabled = !!(el.disabled
        || (el.getAttribute && el.getAttribute('aria-disabled') === 'true'));
      let snippet = '';
      try { snippet = (el.outerHTML || '').replace(/\s+/g, ' ').slice(0, 300); }
      catch (e) { snippet = ''; }
      out.push({
        tag: tag,
        type: type,
        role: role,
        id: (el.id || ''),
        className: (typeof el.className === 'string' ? el.className : ''),
        name: (el.getAttribute && el.getAttribute('name')) || '',
        text: extractLabel(el),
        placeholder: (el.getAttribute && el.getAttribute('placeholder')) || '',
        title: (el.getAttribute && el.getAttribute('title')) || '',
        ariaLabel: aria || '',
        href: (el.getAttribute && el.getAttribute('href')) || '',
        ariaExpanded: (el.getAttribute && el.getAttribute('aria-expanded')) || '',
        ariaCurrent: (el.getAttribute && el.getAttribute('aria-current')) || '',
        childMenuItemCount: el.querySelectorAll
          ? el.querySelectorAll('[role="menuitem"]').length
          : 0,
        childMenuCount: el.querySelectorAll
          ? el.querySelectorAll('[role="menu"]').length
          : 0,
        contentEditable: el.isContentEditable === true,
        disabled: isDisabled,
        visible: visible,
        inViewport: inViewport,
        rect: {
          left: Math.round(rect.left), top: Math.round(rect.top),
          right: Math.round(rect.right), bottom: Math.round(rect.bottom),
          width: Math.round(rect.width), height: Math.round(rect.height),
        },
        center: {
          x: Math.round(rect.left + rect.width / 2),
          y: Math.round(rect.top + rect.height / 2),
        },
        snippet: snippet,
        source: source,
      });
    }
    // 独立发现 open shadow root：shadow host（如 <login-form>/<x-dialog>）
    // 本身往往不是交互元素，不会被 interactiveSelector 命中，因此必须单独
    // 遍历本层全部元素找 .shadowRoot 再递归，否则整个 shadow 子树会被漏扫
    // （见评审：仅在「已命中交互元素」上查 shadowRoot 覆盖不到宿主节点）。
    // depth 上限防极端嵌套导致执行时间失控（兜底路径，宁可漏深层也不卡死）。
    if (depth < 12) {
      let hosts = [];
      try { hosts = Array.from(root.querySelectorAll('*')); }
      catch (e) { hosts = []; }
      for (const h of hosts) {
        if (h && h.shadowRoot) {
          shadowRootsScanned += 1;
          collectFrom(h.shadowRoot, 'shadow', depth + 1);
        }
      }
    }
  };

  try { collectFrom(document, 'document', 0); } catch (e) {}

  try {
    const frames = document.querySelectorAll('iframe');
    if (frames.length) {
      iframeDetected = true;
      for (const f of frames) {
        try { void f.contentDocument; } catch (e) { crossOriginIframe = true; }
      }
    }
  } catch (e) {}

  let candidates = out;
  if (out.length > maxCandidates) {
    const scored = out.map((c) => ({
      c: c,
      pr: (c.visible ? 2 : 0) + (c.inViewport ? 1 : 0) + (c.disabled ? -2 : 0),
    }));
    scored.sort((a, b) => b.pr - a.pr);
    candidates = scored.slice(0, maxCandidates).map((s) => s.c);
  }

  return {
    candidates: candidates,
    total_seen: out.length,
    truncated: out.length > maxCandidates,
    iframe_detected: iframeDetected,
    cross_origin_iframe: crossOriginIframe,
    shadow_roots_scanned: shadowRootsScanned,
    deep_scan_scope: shadowRootsScanned > 0
      ? 'top_document_and_open_shadow'
      : 'top_document_only',
  };
}
"""

# 关键词剥离的后缀（中英）—— 把「登录按钮」「用户名输入框」收敛到核心词。
_KEYWORD_SUFFIXES = (
    "按钮", "输入框", "文本框", "文本", "链接", "页签", "标签", "选项卡",
    "field", "input", "button", "btn", "link", "tab",
)

# 中英同义词词库（硬编码在 resolver，不接项目私表，见方案 §11.1/§18.4）。
_SYNONYM_GROUPS = (
    ("登录", "登陆", "立即登录", "去登录", "登录并继续", "sign in", "signin",
     "log in", "login"),
    ("提交", "确认", "确定", "提交并继续", "submit", "confirm", "ok", "continue",
     "next", "下一步", "继续"),
    ("注册", "立即注册", "免费注册", "sign up", "signup", "register"),
    ("搜索", "查询", "search"),
    ("取消", "返回", "关闭", "cancel", "back", "close"),
)

# 输入类控件的语义判定集合（§8.2：type 只能命中输入类）。
_INPUT_TAGS = {"input", "textarea"}
_INPUT_ROLES = {"textbox", "searchbox", "combobox"}
# 取消/返回/关闭这类「反向词」—— 命中时若目标不属于该组则强负分。
_NEGATIVE_TEXTS = {"取消", "返回", "关闭", "cancel", "back", "close"}


def normalize_keyword(element_name: str) -> str:
    """剥离常见后缀，得到用于匹配的核心关键词（小写）。"""
    kw = (element_name or "").strip().lower()
    for suf in _KEYWORD_SUFFIXES:
        if kw.endswith(suf.lower()) and len(kw) > len(suf):
            kw = kw[: -len(suf)].strip()
            break
    return kw


def compact_text(value: str) -> str:
    """把空格/连字符等分隔差异折叠掉，便于做通用弱归一化匹配。"""
    return re.sub(r"[\s\-_:/]+", "", (value or "").strip().lower())


def expand_synonyms(keyword: str) -> set[str]:
    """把核心关键词扩展为同义词集合（含自身，全部小写）。"""
    kw = (keyword or "").strip().lower()
    result = {kw} if kw else set()
    for group in _SYNONYM_GROUPS:
        low = {g.lower() for g in group}
        if kw and kw in low:
            result |= low
    return result


def is_input_like(cand: dict) -> bool:
    """候选是否为输入类控件（input/textarea/contenteditable/输入类 role）。"""
    tag = (cand.get("tag", "") or "").lower()
    role = (cand.get("role", "") or "").lower()
    if tag in _INPUT_TAGS:
        if tag == "input":
            itype = (cand.get("type", "") or "").lower()
            if itype in {"button", "submit", "reset", "checkbox", "radio",
                         "image", "file", "hidden"}:
                return False
        return True
    if cand.get("contentEditable") is True:
        return True
    return role in _INPUT_ROLES


def _intish(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _menu_tree_stats(cand: dict) -> tuple[int, int]:
    """返回候选下挂菜单数量：(menuitem descendants, menu descendants)."""
    return (
        _intish(
            cand.get("childMenuItemCount", cand.get("descendantMenuItemCount", 0))
        ),
        _intish(cand.get("childMenuCount", cand.get("descendantMenuCount", 0))),
    )


def score_dom_candidate(
    cand: dict, target: str, synonyms: set[str], action: str = "click"
) -> int:
    """对单个 DOM 候选打分（纯函数，见方案 §11.2/§11.3）。

    ``target`` 为核心关键词（小写），``synonyms`` 为其同义词集合（小写）。
    分数越高越可能是目标。负分用于压制 disabled / 不可见 / 反向词等。
    """
    score = 0
    text = (cand.get("text", "") or "").strip().lower()
    aria = (cand.get("ariaLabel", "") or "").strip().lower()
    title = (cand.get("title", "") or "").strip().lower()
    placeholder = (cand.get("placeholder", "") or "").strip().lower()
    blob_id = " ".join(
        str(cand.get(k, "") or "") for k in ("id", "className", "name")
    ).lower()
    tag = (cand.get("tag", "") or "").lower()
    role = (cand.get("role", "") or "").lower()
    itype = (cand.get("type", "") or "").lower()
    href = (cand.get("href", "") or "").strip().lower()
    aria_current = (cand.get("ariaCurrent", "") or "").strip().lower()
    aria_expanded = (cand.get("ariaExpanded", "") or "").strip().lower()
    child_menu_items, child_menus = _menu_tree_stats(cand)
    target_compact = compact_text(target)
    text_compact = compact_text(text)
    aria_compact = compact_text(aria)
    title_compact = compact_text(title)
    placeholder_compact = compact_text(placeholder)
    blob_id_compact = compact_text(blob_id)
    compact_synonyms = {compact_text(s) for s in synonyms if compact_text(s)}

    # ── 正向 ──
    if text and (
        text == target
        or text in synonyms
        or (target_compact and text_compact == target_compact)
        or text_compact in compact_synonyms
    ):
        score += 100
    elif text and any(
        s and (s in text or compact_text(s) in text_compact) for s in synonyms
    ):
        score += 80

    if any(
        s and (
            s in aria
            or s in title
            or s in placeholder
            or compact_text(s) in aria_compact
            or compact_text(s) in title_compact
            or compact_text(s) in placeholder_compact
        )
        for s in synonyms
    ):
        score += 70

    if any(
        s and (s in blob_id or compact_text(s) in blob_id_compact)
        for s in synonyms
    ):
        score += 50

    if tag == "button" or (tag == "input" and itype in {"submit", "button"}):
        score += 20
    if href:
        score += 25
    if aria_current in {"page", "step", "true"}:
        score += 20
    if role == "menuitem" and child_menu_items == 0 and child_menus == 0:
        score += 25

    if cand.get("visible"):
        score += 20
    if not cand.get("disabled"):
        score += 10
    if cand.get("inViewport"):
        score += 10

    # ── 负向 ──
    if cand.get("disabled"):
        score -= 120
    if not cand.get("visible"):
        score -= 100

    rect = cand.get("rect", {}) or {}
    if rect.get("width", 999) < 24 or rect.get("height", 999) < 24:
        score -= 40
    if aria_expanded == "true":
        score -= 80
    if child_menus > 0:
        score -= min(90, 50 + child_menus * 20)
    if child_menu_items > 0:
        score -= min(80, 30 + child_menu_items * 12)

    target_is_negative = target in _NEGATIVE_TEXTS or bool(synonyms & _NEGATIVE_TEXTS)
    if text in _NEGATIVE_TEXTS and not target_is_negative:
        score -= 60

    has_id_name = bool((cand.get("id") or "") or (cand.get("name") or ""))
    if not text and not aria and not has_id_name:
        score -= 30

    if action == "type" and not is_input_like(cand):
        score -= 200

    return score


def _web_tag_name(el: dict) -> str:
    class_name = (el.get("className", "") or "").strip().lower()
    return class_name.split(":", 1)[0] if class_name else ""


def _web_text_match_score(el: dict, query: str, exact: bool, field: str = "text") -> int:
    """Web 文本候选打分：优先精确/紧凑命中，压低长拼接容器。"""
    query_raw = (query or "").strip().lower()
    query_compact = compact_text(query_raw)
    if not query_raw:
        return -1

    values: list[tuple[str, bool]] = []
    primary = (el.get(field, "") or "").strip().lower() if field != "text" else ""
    if primary:
        values.append((primary, False))

    el_text = (el.get("text", "") or "").strip().lower()
    if el_text and (field == "text" or el_text != primary):
        values.append((el_text, False))

    class_name = (el.get("className", "") or "").strip().lower()
    if class_name:
        values.append((class_name, True))

    role = (el.get("role", "") or "").strip().lower()
    href = (el.get("href", "") or "").strip().lower()
    aria_current = (el.get("ariaCurrent", "") or "").strip().lower()
    aria_expanded = (el.get("ariaExpanded", "") or "").strip().lower()
    child_menu_items, child_menus = _menu_tree_stats(el)

    best = -1
    pattern = re.compile(
        f"^{re.escape(query_raw)}$" if exact else re.escape(query_raw),
        re.IGNORECASE,
    )
    for value, is_class_name in values:
        value_compact = compact_text(value)
        base = -1
        if exact:
            if value == query_raw:
                base = 240
            elif query_compact and value_compact == query_compact:
                base = 230
        else:
            if value == query_raw:
                base = 220
            elif query_compact and value_compact == query_compact:
                base = 210
            elif pattern.search(value):
                base = 170
            elif query_compact and query_compact in value_compact:
                base = 160
        if base < 0:
            continue

        if is_class_name:
            base -= 80

        compact_gap = abs(len(value_compact) - len(query_compact))
        base -= min(compact_gap, 40)

        tag = _web_tag_name(el)
        if tag in {"button", "input", "textarea", "a", "span", "label"}:
            base += 20
        elif tag in {"div", "li", "ul", "nav"}:
            base -= 20
        if href:
            base += 20
        if aria_current in {"page", "step", "true"}:
            base += 15
        if role == "menuitem" and child_menu_items == 0 and child_menus == 0:
            base += 30
        if aria_expanded == "true":
            base -= 90
        if child_menus > 0:
            base -= min(110, 60 + child_menus * 20)
        if child_menu_items > 0:
            base -= min(90, 30 + child_menu_items * 12)

        if len(value) > max(40, len(query_raw) * 4):
            base -= min(80, len(value) - max(40, len(query_raw) * 4))
        if len(value.split()) > 6:
            base -= 20

        bounds = el.get("boundsInScreen", {}) or {}
        width = max(0, int(bounds.get("right", 0)) - int(bounds.get("left", 0)))
        height = max(0, int(bounds.get("bottom", 0)) - int(bounds.get("top", 0)))
        area = width * height
        if area > 300000:
            base -= 50
        elif area > 150000:
            base -= 25

        best = max(best, base)
    return best


def _candidate_brief(cand: dict) -> dict:
    """从候选里抽取用于诊断/日志的精简信息（不含大字段如 snippet）。"""
    return {
        "text": cand.get("text", ""),
        "tag": cand.get("tag", ""),
        "type": cand.get("type", ""),
        "id": cand.get("id", ""),
        "disabled": bool(cand.get("disabled")),
        "visible": bool(cand.get("visible")),
        "inViewport": bool(cand.get("inViewport")),
        "ariaExpanded": cand.get("ariaExpanded", ""),
        "childMenuItemCount": _menu_tree_stats(cand)[0],
        "childMenuCount": _menu_tree_stats(cand)[1],
    }


class LocatorResolver:
    """元素定位解析器。缓存优先 → 平台过滤 → 逐策略尝试 → 命中写缓存。"""

    def __init__(self, driver, state_provider, cache: ElementCache) -> None:
        self.driver = driver
        self.state_provider = state_provider
        self.cache = cache
        self._platform = str(getattr(driver, "platform", "web")).lower()
        self._available_strategies = PLATFORM_STRATEGIES.get(
            self._platform, PLATFORM_STRATEGIES["web"]
        )
        # 当前 UIState 的坐标缩放因子（缓存命中时用于换算）
        self._scale_x = 1.0
        self._scale_y = 1.0

    def attach_state_provider(self, state_provider) -> None:
        """把 resolver 的状态源切换到包装后的 provider。

        装配顺序问题：``LocatorResolver`` 必须先于 ``CachedStateProvider``
        构造（后者依赖前者做 scale 同步），因此初次只能拿到「内层」provider。
        包装完成后必须调用本方法回指，否则 resolver 的 text/spatial 路径会
        直接读旧 provider，既绕开 PEL 缓存，又与 Phase A 重试刷新的 provider
        不在同一份状态源上（见评审 #1）。
        """
        self.state_provider = state_provider

    # ── 缩放因子同步 ──────────────────────────────────────────────────

    def update_scale(self, scale_x: float, scale_y: float) -> None:
        """记录当前 UIState 的坐标缩放因子（仅作信息记录）。

        注意：page_action 通过 ``driver.tap(x, y)`` 直接下发坐标，而 Web/原生
        的 tap 输入空间就是元素 bounds（boundsInScreen）/ CSS getBoundingClientRect
        所在的「原始视口/设备像素」空间，与 ``coordinate_scale``（仅影响发给
        模型的截图缩放）无关。缓存按 viewport 维度隔离，同一 viewport 下坐标
        空间一致，因此定位坐标全程以原始像素存取，不做 scale 换算。
        """
        self._scale_x = scale_x or 1.0
        self._scale_y = scale_y or 1.0

    def _viewport(self) -> dict:
        vp = getattr(self.driver, "_viewport", {}) or {}
        return {"width": vp.get("width", 0), "height": vp.get("height", 0)}

    # ── 滚动到元素（Web 真实滚动）─────────────────────────────────────

    async def scroll_into_view(self, selectors: list[dict]) -> bool:
        """把元素滚动进可视区（仅 Web，用 scrollIntoView）。

        返回是否成功滚动。非 Web 平台或无 css 选择器时返回 False
        （由调用方决定如何兜底）。
        """
        if self._platform != "web" or not hasattr(self.driver, "evaluate"):
            return False
        normalized = normalize_selectors(selectors)
        for strat in normalized:
            if strat["type"] != Strategy.CSS_SELECTOR:
                continue
            css = strat.get("value", "")
            if not css:
                continue
            scroll_js = (
                "(css) => { let el = null;"
                "  if (css.startsWith('xpath=')) {"
                "    const r = document.evaluate(css.slice(6), document, null,"
                "      XPathResult.FIRST_ORDERED_NODE_TYPE, null); el = r.singleNodeValue;"
                "  } else { try { el = document.querySelector(css); } catch(e){ el=null; } }"
                "  if (!el) return false;"
                "  el.scrollIntoView({block:'center', inline:'center'}); return true; }"
            )
            try:
                ok = await self.driver.evaluate(
                    f"({scroll_js})({json.dumps(css)})"
                )
                if ok:
                    return True
            except Exception as e:  # noqa: BLE001
                logger.debug("scrollIntoView failed for %s: %s", css, e)
        return False

    # ── DOM 深度扫描（Phase B，仅 Web/H5）────────────────────────────

    async def deep_scan_dom_candidates(
        self, max_candidates: int = 200, timeout: float = 3.0
    ) -> tuple[list[dict], dict]:
        """注入 ``DEEP_DOM_SCAN_JS`` 抓取运行时 DOM 候选。

        返回 ``(candidates, meta)``。``meta`` 携带诊断字段（total_seen /
        truncated / iframe_detected / cross_origin_iframe / unavailable /
        reason）。任何执行异常或超时都不抛出，而是返回空候选 + 诊断，
        保证不阻塞主链路（见方案 §10.3/§10.5）。
        """
        meta: dict = {
            "total_seen": 0,
            "truncated": False,
            "iframe_detected": False,
            "cross_origin_iframe": False,
            "shadow_roots_scanned": 0,
            # 默认 top_document_only 仅在「未执行/超时/注入失败」时成立 —— 那些
            # 情况下确实没扫到 shadow；真正执行后由 JS 返回的实际范围覆盖。
            "deep_scan_scope": "top_document_only",
        }
        if self._platform != "web" or not hasattr(self.driver, "evaluate"):
            meta["unavailable"] = True
            meta["reason"] = "not_supported_on_native"
            return [], meta

        call = (
            f"({DEEP_DOM_SCAN_JS})("
            f"{json.dumps(INTERACTIVE_SELECTOR)}, {int(max_candidates)})"
        )
        try:
            raw = await asyncio.wait_for(self.driver.evaluate(call), timeout=timeout)
        except asyncio.TimeoutError:
            meta["unavailable"] = True
            meta["reason"] = "deep_scan_timeout"
            return [], meta
        except Exception as e:  # noqa: BLE001 — 注入受限/执行异常一律降级
            meta["unavailable"] = True
            meta["reason"] = "deep_scan_injection_blocked"
            logger.debug("deep_scan_dom evaluate failed: %s", e)
            return [], meta

        if not isinstance(raw, dict):
            meta["unavailable"] = True
            meta["reason"] = "deep_scan_injection_blocked"
            return [], meta

        candidates = raw.get("candidates") or []
        meta.update(
            {
                "total_seen": raw.get("total_seen", len(candidates)),
                "truncated": bool(raw.get("truncated")),
                "iframe_detected": bool(raw.get("iframe_detected")),
                "cross_origin_iframe": bool(raw.get("cross_origin_iframe")),
                "shadow_roots_scanned": int(raw.get("shadow_roots_scanned", 0) or 0),
                "deep_scan_scope": raw.get("deep_scan_scope", "top_document_only"),
            }
        )
        return candidates, meta

    async def resolve_via_dom_deep_scan(
        self,
        *,
        element_name: str,
        action: str = "click",
        min_score: int = 100,
        ambiguous_gap: int = 15,
        cache_ttl: float = 5.0,
        max_candidates: int = 200,
        timeout: float = 3.0,
    ) -> LocatorResult:
        """Phase B 主入口：抓取候选 → 评分 → 阈值/歧义判定 → 命中写短 TTL 缓存。

        始终返回 ``LocatorResult`` 并填充 ``diagnostics``，绝不抛异常
        （见方案 §10.5/§12.5）。
        """
        candidates, meta = await self.deep_scan_dom_candidates(
            max_candidates=max_candidates, timeout=timeout
        )
        diag: dict = {
            "strategy": Strategy.DOM_DEEP_SCAN.name,
            "platform": self._platform,
            "candidates_seen": meta.get("total_seen", 0),
            **{
                k: meta[k]
                for k in ("iframe_detected", "cross_origin_iframe",
                          "deep_scan_scope", "shadow_roots_scanned", "truncated")
                if k in meta
            },
        }

        if meta.get("unavailable"):
            diag["reason"] = meta.get("reason", "deep_scan_unavailable")
            return LocatorResult(
                success=False,
                error=f"deep scan unavailable: {diag['reason']}",
                diagnostics=diag,
            )

        if not candidates:
            diag["reason"] = "no_candidate"
            return LocatorResult(
                success=False, error="deep scan found no candidate", diagnostics=diag
            )

        keyword = normalize_keyword(element_name)
        synonyms = expand_synonyms(keyword)
        scored = [
            (score_dom_candidate(c, keyword, synonyms, action), c) for c in candidates
        ]
        scored.sort(key=lambda t: t[0], reverse=True)

        top1_score, top1 = scored[0]
        top2_score = scored[1][0] if len(scored) > 1 else None
        gap = top1_score - top2_score if top2_score is not None else top1_score

        diag.update(
            {
                "top1": _candidate_brief(top1),
                "top1_score": top1_score,
                "top2_score": top2_score,
                "score_gap": gap,
            }
        )

        # type 动作：top1 必须是输入类控件，否则即使语义命中也不自动输入（§8.2）
        if action == "type" and not is_input_like(top1):
            diag["reason"] = "not_input_like"
            return LocatorResult(
                success=False,
                error="deep scan top candidate is not an input control",
                diagnostics=diag,
            )

        if top1.get("disabled"):
            diag["reason"] = "top1_disabled"
            return LocatorResult(
                success=False,
                error="deep scan top candidate is disabled",
                diagnostics=diag,
            )

        if top1_score < min_score:
            diag["reason"] = "score_below_threshold"
            return LocatorResult(
                success=False,
                error=f"deep scan top score {top1_score} < {min_score}",
                diagnostics=diag,
            )

        if top2_score is not None and gap < ambiguous_gap:
            diag["reason"] = "ambiguous"
            diag["ambiguous"] = True
            return LocatorResult(
                success=False,
                error=f"deep scan ambiguous (gap={gap} < {ambiguous_gap})",
                diagnostics=diag,
            )

        center = top1.get("center", {}) or {}
        x = int(center.get("x", 0))
        y = int(center.get("y", 0))
        diag["reason"] = "matched"
        diag["score"] = top1_score
        diag["candidate"] = _candidate_brief(top1)
        diag["source"] = top1.get("source", "document")

        result = LocatorResult(
            success=True, x=x, y=y, strategy_used=Strategy.DOM_DEEP_SCAN,
            element_info=top1, diagnostics=diag,
        )

        # 命中写短 TTL 缓存，但仅在「稳态」候选上（见方案 §12.2/§12.3）
        cacheable = (
            element_name
            and top1.get("visible")
            and not top1.get("disabled")
            and top1.get("inViewport")
            and top1_score >= 110
            and top1.get("source") == "document"
        )
        if cacheable:
            self.cache.set(
                element_name, x, y, Strategy.DOM_DEEP_SCAN.name,
                ttl=cache_ttl, viewport=self._viewport(),
            )
            diag["cached_written"] = True
        else:
            diag["cached_written"] = False

        return result

    # ── 策略过滤 ──────────────────────────────────────────────────────

    def _filter_strategies(self, strategies: list[dict]) -> list[dict]:
        """移除当前平台不可用的策略，并按 Strategy 优先级排序。"""
        valid = [s for s in strategies if s["type"] in self._available_strategies]
        valid.sort(key=lambda s: int(s["type"]))
        return valid

    # ── 主入口 ────────────────────────────────────────────────────────

    async def resolve(
        self,
        selectors: list[dict],
        element_name: str = "",
    ) -> LocatorResult:
        """按优先级逐个尝试定位策略。selectors 支持短格式与规范格式。"""
        viewport = self._viewport()

        # Step 1: 缓存检查（viewport-aware）。坐标为原始像素，tap 直接消费，
        # 不做 scale 换算（见 update_scale 的说明）。
        if element_name:
            cached = self.cache.get(element_name, viewport)
            if cached is not None:
                try:
                    strat_enum = Strategy[cached.strategy]
                except KeyError:
                    strat_enum = Strategy.VISION_FALLBACK
                return LocatorResult(
                    success=True, x=cached.x, y=cached.y,
                    strategy_used=strat_enum, cache_hit=True,
                )

        # Step 2: 归一化 + 平台过滤
        normalized = normalize_selectors(selectors)
        valid = self._filter_strategies(normalized)
        if not valid:
            return LocatorResult(
                success=False,
                error=f"No applicable strategy for '{element_name}' on "
                f"platform '{self._platform}'",
            )

        # Step 3: 逐个尝试
        result = await self._try_strategies_raw(valid)

        # Step 4: 命中写缓存（坐标即原始像素，直接存）
        if result.success and element_name:
            ttl = None
            for s in valid:
                if s["type"] == result.strategy_used and s.get("ttl"):
                    ttl = s["ttl"]
                    break
            self.cache.set(
                element_name, result.x, result.y,
                result.strategy_used.name, ttl=ttl, viewport=viewport,
            )
            return result

        if result.success:
            return result

        return LocatorResult(
            success=False,
            error=f"All {len(valid)} strategies exhausted for "
            f"'{element_name}': {result.error}",
        )

    async def _try_strategies_raw(self, valid: list[dict]) -> LocatorResult:
        """绕过缓存，按顺序尝试已归一化的策略列表。"""
        last_error = ""
        for strat in valid:
            result = await self._try_strategy(strat)
            if result.success:
                return result
            last_error = result.error
        return LocatorResult(success=False, error=last_error or "no strategy matched")

    async def _try_strategy(self, strat: dict) -> LocatorResult:
        """尝试单个策略（平台感知分发）。"""
        stype = strat["type"]
        value = strat.get("value", "")

        if stype == Strategy.CSS_SELECTOR:
            return await self._try_css(value, strat)
        if stype == Strategy.ACCESSIBILITY_ID:
            return await self._try_accessibility_id(value)
        if stype in (Strategy.TEXT_EXACT, Strategy.TEXT_FUZZY):
            return await self._try_text(value, exact=(stype == Strategy.TEXT_EXACT))
        if stype == Strategy.PLACEHOLDER:
            return await self._try_text(value, exact=False, field="placeholder")
        if stype == Strategy.SPATIAL:
            return await self._try_spatial(value, strat)
        if stype == Strategy.VISION_FALLBACK:
            return await self._try_vision(value)
        return LocatorResult(success=False, error=f"Unknown strategy: {stype}")

    # ── CSS 策略（Web 专用）──────────────────────────────────────────

    async def _try_css(self, css: str, strat: dict) -> LocatorResult:
        if self._platform != "web" or not hasattr(self.driver, "evaluate"):
            return LocatorResult(
                success=False,
                error=f"CSS strategy not available on platform '{self._platform}'",
            )
        index = strat.get("index")
        js_arg = json.dumps({"css": css, "index": index})
        try:
            raw = await self.driver.evaluate(
                f"({CSS_QUERY_JS})({js_arg}, {json.dumps(INTERACTIVE_SELECTOR)})"
            )
        except Exception as e:  # noqa: BLE001
            return LocatorResult(success=False, error=f"CSS query failed: {e}")
        if raw and isinstance(raw, dict) and raw.get("x") is not None:
            return LocatorResult(
                success=True, x=int(raw["x"]), y=int(raw["y"]),
                strategy_used=Strategy.CSS_SELECTOR, element_info=raw,
            )
        return LocatorResult(
            success=False, error=f"CSS selector '{css}' matched no visible element"
        )

    # ── TEXT 策略（Web 独立 / 原生复用 element_search）────────────────

    async def _try_text(
        self, text: str, exact: bool = False, field: str = "text"
    ) -> LocatorResult:
        ui_state = await self.state_provider.get_state()
        elements = ui_state.elements
        if self._platform == "web":
            return self._match_text_web(elements, text, exact, field)
        return self._match_text_native(elements, text, exact)

    def _match_text_web(
        self, elements: list[dict], text: str, exact: bool, field: str = "text"
    ) -> LocatorResult:
        """Web 平台独立文本匹配（候选打分，避免首个容器误命中）。"""
        text_lower = text.lower().strip()
        if not text_lower:
            return LocatorResult(success=False, error="Empty text query")

        best = None
        best_score = -1
        for el in self._flatten_elements(elements):
            checked = el.get("checkedState", "") or ""
            if "disabled" in checked:
                continue
            score = _web_text_match_score(el, text_lower, exact, field)
            if score > best_score:
                best = el
                best_score = score

        if best:
            x, y = self._center(best)
            return LocatorResult(
                success=True, x=x, y=y,
                strategy_used=Strategy.TEXT_EXACT if exact else Strategy.TEXT_FUZZY,
                element_index=best.get("index"), element_info=best,
            )
        return LocatorResult(
            success=False, error=f"Text '{text}' not found in Web elements"
        )

    def _match_text_native(
        self, elements: list[dict], text: str, exact: bool
    ) -> LocatorResult:
        """Android/iOS: 复用 element_search.Filters.text_matches()。"""
        from mobilerun.tools.helpers.element_search import Filters

        matcher = Filters.text_matches(text)
        matched = matcher(elements if isinstance(elements, list) else [elements])
        if matched:
            el = matched[0]
            x, y = self._center(el)
            return LocatorResult(
                success=True, x=x, y=y,
                strategy_used=Strategy.TEXT_EXACT if exact else Strategy.TEXT_FUZZY,
                element_index=el.get("index"), element_info=el,
            )
        return LocatorResult(success=False, error=f"Text '{text}' not found")

    # ── SPATIAL 策略 ─────────────────────────────────────────────────

    async def _try_spatial(self, description: str, strat: dict) -> LocatorResult:
        if self._platform == "web":
            ui_state = await self.state_provider.get_state()
            return self._match_spatial_web(ui_state, description, strat)
        return await self._match_spatial_native(description, strat)

    def _match_spatial_web(
        self, ui_state, description: str, strat: dict
    ) -> LocatorResult:
        """Web 独立空间关系匹配。支持 'below:锚点' 等前缀语法。"""
        elements = ui_state.elements
        desc = description.strip()
        desc_lower = desc.lower()
        viewport = self._viewport()

        for prefix in ("below:", "above:", "right_of:", "left_of:"):
            if desc_lower.startswith(prefix):
                anchor_name = desc[len(prefix):].strip().strip("'\"")
                anchor_bounds = self._find_anchor_bounds(
                    elements, anchor_name, viewport
                )
                if anchor_bounds:
                    return self._spatial_filter_web(
                        elements, anchor_bounds, prefix.rstrip(":"), anchor_name
                    )
                return LocatorResult(
                    success=False, error=f"Anchor element '{anchor_name}' not found"
                )

        return LocatorResult(
            success=False,
            error=f"Spatial description '{description}' requires vision fallback",
        )

    def _find_anchor_bounds(
        self, elements: list[dict], anchor_name: str, viewport: dict
    ) -> Optional[dict]:
        cache_entry = self.cache.get(anchor_name, viewport)
        if cache_entry is not None:
            return {
                "left": cache_entry.x - 50, "right": cache_entry.x + 50,
                "top": cache_entry.y - 20, "bottom": cache_entry.y + 20,
            }
        for el in self._flatten_elements(elements):
            if anchor_name.lower() in (el.get("text", "") or "").lower():
                return el.get("boundsInScreen", {})
        return None

    def _spatial_filter_web(
        self, elements: list[dict], anchor: dict, relation: str, anchor_name: str
    ) -> LocatorResult:
        candidates = []
        for el in self._flatten_elements(elements):
            b = el.get("boundsInScreen", {})
            if not b:
                continue
            if (el.get("text", "") or "").lower() == anchor_name.lower():
                continue
            if relation == "below" and b.get("top", 0) > anchor.get("bottom", 0):
                dist = abs(b.get("left", 0) - anchor.get("left", 0))
                candidates.append((dist, el))
            elif relation == "above" and b.get("bottom", 0) < anchor.get("top", 0):
                dist = abs(b.get("left", 0) - anchor.get("left", 0))
                candidates.append((dist, el))
            elif relation == "right_of" and b.get("left", 0) > anchor.get("right", 0):
                dist = abs(b.get("top", 0) - anchor.get("top", 0))
                candidates.append((dist, el))
            elif relation == "left_of" and b.get("right", 0) < anchor.get("left", 0):
                dist = abs(b.get("top", 0) - anchor.get("top", 0))
                candidates.append((dist, el))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            best = candidates[0][1]
            x, y = self._center(best)
            return LocatorResult(
                success=True, x=x, y=y,
                strategy_used=Strategy.SPATIAL,
                element_index=best.get("index"), element_info=best,
            )
        return LocatorResult(
            success=False, error=f"No element found {relation} '{anchor_name}'"
        )

    async def _match_spatial_native(
        self, description: str, strat: dict
    ) -> LocatorResult:
        """Android/iOS: 复用 element_search.Filters 空间关系方法。

        支持 'below:锚点文本' 等前缀语法：先 text 找锚点再做空间筛选。
        """
        from mobilerun.tools.helpers.element_search import Filters

        desc = description.strip()
        desc_lower = desc.lower()
        relation_map = {
            "below:": Filters.below,
            "above:": Filters.above,
            "right_of:": Filters.right_of,
            "left_of:": Filters.left_of,
        }
        for prefix, builder in relation_map.items():
            if desc_lower.startswith(prefix):
                anchor_name = desc[len(prefix):].strip().strip("'\"")
                ui_state = await self.state_provider.get_state()
                anchor_filter = Filters.text_matches(anchor_name)
                matched = builder(anchor_filter)(ui_state.elements)
                if matched:
                    el = matched[0]
                    x, y = self._center(el)
                    return LocatorResult(
                        success=True, x=x, y=y, strategy_used=Strategy.SPATIAL,
                        element_index=el.get("index"), element_info=el,
                    )
                return LocatorResult(
                    success=False, error=f"No element found {prefix} '{anchor_name}'"
                )
        return LocatorResult(
            success=False,
            error="Spatial search for native requires 'relation:anchor' syntax",
        )

    # ── ACCESSIBILITY_ID 策略（Android/iOS 专用）────────────────────

    async def _try_accessibility_id(self, aid: str) -> LocatorResult:
        if self._platform == "web":
            return LocatorResult(
                success=False, error="ACCESSIBILITY_ID not available on Web"
            )
        from mobilerun.tools.helpers.element_search import Filters

        ui_state = await self.state_provider.get_state()
        matcher = Filters.id_matches(aid)
        matched = matcher(ui_state.elements)
        if matched:
            el = matched[0]
            x, y = self._center(el)
            return LocatorResult(
                success=True, x=x, y=y, strategy_used=Strategy.ACCESSIBILITY_ID,
                element_index=el.get("index"), element_info=el,
            )
        return LocatorResult(success=False, error=f"ID '{aid}' not found")

    # ── VISION 最终回退 ─────────────────────────────────────────────

    async def _try_vision(self, description: str) -> LocatorResult:
        """截图 + LLM 识别（最后手段）。

        当前 page_action 在所有确定性策略失败后由调用方降级到 index/坐标
        操作，因此这里只返回失败信号，让 Agent 走传统 click(index) 兜底。
        """
        return LocatorResult(
            success=False,
            error="Vision fallback unavailable; fall back to click(index)",
        )

    # ── 工具方法 ─────────────────────────────────────────────────────

    @staticmethod
    def _center(el: dict) -> tuple[int, int]:
        b = el.get("boundsInScreen", {}) or {}
        if b:
            x = (b.get("left", 0) + b.get("right", 0)) // 2
            y = (b.get("top", 0) + b.get("bottom", 0)) // 2
            return x, y
        bounds_str = el.get("bounds", "")
        if bounds_str:
            try:
                left, top, right, bottom = (int(float(p)) for p in bounds_str.split(","))
                return (left + right) // 2, (top + bottom) // 2
            except (ValueError, TypeError):
                pass
        return 0, 0

    @staticmethod
    def _flatten_elements(elements: list[dict]) -> list[dict]:
        result = []
        for el in elements or []:
            result.append(el)
            children = el.get("children")
            if children:
                result.extend(LocatorResolver._flatten_elements(children))
        return result
