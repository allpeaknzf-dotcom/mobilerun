"""PEL — page_action 语义化页面操作工具。

与现有 ``click(index=N)`` 并存，互不冲突。Agent 可自行选择。

定位坐标语义：``LocatorResolver`` 返回的 (x, y) 是「原始视口/设备像素」，
正好是 ``driver.tap`` 在 Web/Android/iOS 上的输入空间，因此直接下发，
不经过 ``convert_point``（那是给模型视觉坐标契约用的）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from mobilerun.agent.action_result import ActionResult
from mobilerun.element.locator import LocatorResult, Strategy

if TYPE_CHECKING:
    from mobilerun.agent.action_context import ActionContext

logger = logging.getLogger("mobilerun")

_VALID_ACTIONS = {"click", "type", "scroll_to", "wait_for", "verify"}

# 混合模式参数（见方案 §16）。先硬编码在实现层，待真实回归数据支撑后再配置化。
_STANDARD_ATTEMPTS = 4
_RETRY_DELAYS = [0.4, 0.8, 1.2]
# Phase B 仅对这些 action 触发深度扫描（scroll_to 不接，见方案 §8.4）。
_DEEP_SCAN_ACTIONS = {"click", "type", "wait_for", "verify"}


def _platform_of(ctx: "ActionContext") -> str:
    """从 driver 取小写平台名（web/android/ios）。"""
    return str(getattr(getattr(ctx, "driver", None), "platform", "")).lower()


def _current_page(ctx: "ActionContext") -> Optional[dict]:
    """优先用 CachedStateProvider 已匹配/发现的页面；否则当场用 registry 匹配。"""
    sp = getattr(ctx, "state_provider", None)
    page = getattr(sp, "current_page", None)
    if page:
        return page
    return None


def _resolve_selectors(
    ctx: "ActionContext", element: str, page_def: Optional[dict]
) -> list[dict]:
    """获取元素定位策略列表。

    页面定义 selector 优先，但不会阻断语义文本回退：
    - 若页面定义存在 detected_selectors，先按原顺序使用；
    - 若其中尚未包含等价的文本 selector，则追加一个 TEXT_FUZZY(element)
      作为兜底，避免脏/过期 selector 让定位提前失败；
    - 无预定义时，直接用元素名做 text 匹配。
    """
    fallback = {"type": Strategy.TEXT_FUZZY, "value": element}
    if page_def:
        el = page_def.get("elements", {}).get(element)
        if el:
            selectors = el.get("detected_selectors", [])
            if selectors:
                resolved = list(selectors)
                has_equivalent_text = any(
                    (
                        s.get("type") == Strategy.TEXT_FUZZY
                        and s.get("value") == element
                    )
                    or s.get("text") == element
                    or s.get("text_exact") == element
                    for s in resolved
                    if isinstance(s, dict)
                )
                if not has_equivalent_text:
                    resolved.append(fallback)
                return resolved
    return [fallback]


def _mark_state_dirty(ctx: "ActionContext") -> None:
    """交互后标记状态缓存失效，确保下一次页面读取走完整探测。"""
    sp = getattr(ctx, "state_provider", None)
    if sp is None:
        return
    mark_dirty = getattr(sp, "mark_dirty", None)
    if callable(mark_dirty):
        try:
            mark_dirty()
        except Exception as e:  # noqa: BLE001
            logger.debug("mark_dirty failed: %s", e)


async def _refresh_state_for_retry(ctx: "ActionContext") -> None:
    """重试前强制刷新页面状态，并等待 provider 内部同步 ``current_page``。

    平台分流（见方案 §7.2 的「内部刷新辅助入约」）：

    - provider 提供 ``refresh_state()``：Web 用 ``force_full=True`` 绕开缓存
      快路径（否则是「伪重试」）；Android/iOS 用 ``force_full=False``。
    - provider 不提供 ``refresh_state()``：回退到 ``get_state()``。

    page_action 不通过 ``isinstance`` 判别 provider 具体类型，仅靠平台名决定
    是否 ``force_full``，从而避免与 CachedStateProvider 实现绑死。
    """
    sp = getattr(ctx, "state_provider", None)
    if sp is None:
        return
    refresh = getattr(sp, "refresh_state", None)
    if callable(refresh):
        force_full = _platform_of(ctx) == "web"
        try:
            await refresh(force_full=force_full)
            return
        except Exception as e:  # noqa: BLE001 — 刷新失败不应中断重试
            logger.debug("refresh_state failed, fallback to get_state: %s", e)
    get_state = getattr(sp, "get_state", None)
    if callable(get_state):
        try:
            await get_state()
        except Exception as e:  # noqa: BLE001
            logger.debug("get_state during retry failed: %s", e)


async def _resolve_with_standard_retries(
    ctx: "ActionContext", action: str, element: str
) -> tuple[LocatorResult, dict]:
    """Phase A：常规定位多轮重试（App + Web 通用）。

    每轮都重新读取 ``current_page`` 并重算 selectors（页面可能在刷新后才
    匹配/发现，见方案 §9.2）。失败后 sleep + 强制刷新再进下一轮。
    """
    trace: dict = {"attempts": []}
    last_result: Optional[LocatorResult] = None

    for i in range(_STANDARD_ATTEMPTS):
        page_def = _current_page(ctx)
        selectors = _resolve_selectors(ctx, element, page_def)

        if action == "scroll_to":
            # Web 增强：先尝试真实滚动进可视区；非 Web 返回 False 视为 no-op
            try:
                await ctx.locator_resolver.scroll_into_view(selectors)
            except Exception as e:  # noqa: BLE001
                logger.debug("scroll_into_view failed: %s", e)

        result = await ctx.locator_resolver.resolve(selectors, element_name=element)
        trace["attempts"].append(
            {
                "attempt": i + 1,
                "page": page_def.get("name") if page_def else "",
                "selector_count": len(selectors),
                "selector_source": (
                    "page_def+fallback_text_fuzzy"
                    if page_def and len(selectors) > 1
                    else "page_def"
                    if page_def
                    else "fallback_text_fuzzy"
                ),
                "success": result.success,
                "error": result.error,
                "strategy": result.strategy_used.name if result.success else "",
            }
        )

        if result.success:
            return result, trace

        last_result = result
        if i < _STANDARD_ATTEMPTS - 1:
            delay = _RETRY_DELAYS[i] if i < len(_RETRY_DELAYS) else _RETRY_DELAYS[-1]
            await asyncio.sleep(delay)
            await _refresh_state_for_retry(ctx)

    return last_result or LocatorResult(success=False, error="no attempt run"), trace


async def _resolve_with_hybrid_recovery(
    ctx: "ActionContext", action: str, element: str
) -> tuple[LocatorResult, dict]:
    """混合模式主控：Phase A 常规重试 →（仅 Web）Phase B DOM 深度扫描。

    返回 ``(LocatorResult, trace)``。trace 结构见方案 §12.4，含
    ``standard_attempts`` 与 ``deep_scan`` 两段，用于最终失败摘要。
    """
    result, std_trace = await _resolve_with_standard_retries(ctx, action, element)
    trace: dict = {"standard_attempts": std_trace["attempts"]}

    if result.success:
        trace["deep_scan"] = {"executed": False, "reason": "standard_succeeded"}
        return result, trace

    platform = _platform_of(ctx)
    resolver = ctx.locator_resolver

    # Phase B 触发条件（见方案 §10.1）：Web + action 在白名单 + driver 可注入
    can_deep_scan = (
        platform == "web"
        and action in _DEEP_SCAN_ACTIONS
        and hasattr(getattr(ctx, "driver", None), "evaluate")
        and hasattr(resolver, "resolve_via_dom_deep_scan")
    )
    if not can_deep_scan:
        reason = (
            "not_supported_on_native"
            if platform != "web"
            else "action_not_eligible"
        )
        trace["deep_scan"] = {"executed": False, "reason": reason}
        return result, trace

    deep_result = await resolver.resolve_via_dom_deep_scan(
        element_name=element, action=action
    )
    diag = deep_result.diagnostics or {}
    if deep_result.success:
        trace["deep_scan"] = {
            "executed": True,
            "candidates_seen": diag.get("candidates_seen", 0),
            "top1_score": diag.get("score") or diag.get("top1_score"),
            "top2_score": diag.get("top2_score"),
            "reason": diag.get("reason", "matched"),
            "top_candidate": diag.get("candidate") or diag.get("top1"),
            "cached_written": diag.get("cached_written", False),
        }
        return deep_result, trace

    # 深度扫描失败/不可用：区分 executed 与 unavailable（超时/注入受限）
    reason = diag.get("reason", "no_candidate")
    unavailable = reason in ("deep_scan_timeout", "deep_scan_injection_blocked",
                             "not_supported_on_native")
    trace["deep_scan"] = {
        "executed": not unavailable,
        "candidates_seen": diag.get("candidates_seen", 0),
        "top1_score": diag.get("top1_score"),
        "top2_score": diag.get("top2_score"),
        "reason": reason,
        "top_candidate": diag.get("top1"),
    }
    # 把深扫诊断挂到返回结果上，供失败摘要使用
    result.diagnostics = diag
    return result, trace


def _build_failure_summary(element: str, trace: dict) -> str:
    """把混合恢复 trace 压缩成可读的最终失败摘要（见方案 §13）。"""
    std = trace.get("standard_attempts", []) or []
    attempts = len(std)
    last_error = std[-1].get("error", "") if std else "no attempt"
    deep = trace.get("deep_scan", {}) or {}

    line = (
        f"Could not locate '{element}'.\n"
        f"standard attempts={attempts}, last_error={last_error}"
    )

    if not deep.get("executed"):
        reason = deep.get("reason", "skipped")
        if reason in ("deep_scan_timeout", "deep_scan_injection_blocked"):
            line += f";\ndeep_scan=unavailable, reason={reason}."
        elif reason == "not_supported_on_native":
            line += ";\ndeep_scan=skipped, reason=not_supported_on_native."
        else:
            line += f";\ndeep_scan=skipped, reason={reason}."
    else:
        top = deep.get("top_candidate") or {}
        top_text = top.get("text", "") if isinstance(top, dict) else ""
        line += (
            f";\ndeep_scan=executed, candidates={deep.get('candidates_seen', 0)}, "
            f"top1={top_text or 'n/a'}(score={deep.get('top1_score')}"
        )
        if isinstance(top, dict) and top.get("disabled"):
            line += ", disabled=true"
        line += f"), reason={deep.get('reason', 'unknown')}."

    return (
        line
        + "\nIf this happened after entering the wrong section, use "
        "system_button(back) once and retry the explicit target."
        "\nFall back to click(index=N)."
    )


async def page_action(
    action: str,
    element: str = "",
    value: str = "",
    invalidate_after: Optional[list] = None,
    *,
    ctx: "ActionContext",
) -> ActionResult:
    """语义化页面操作。

    action: 'click' | 'type' | 'scroll_to' | 'wait_for' | 'verify'
    element: 语义元素名，如 '登录按钮'
    value: type 时的输入值
    invalidate_after: 操作后失效缓存的元素名列表（AJAX 刷新补偿）
    """
    action = (action or "").strip().lower()
    if action not in _VALID_ACTIONS:
        return ActionResult(
            success=False,
            summary=f"Unknown page_action '{action}'. Valid: {sorted(_VALID_ACTIONS)}",
        )
    if not element:
        return ActionResult(success=False, summary="page_action requires 'element'")

    resolver = getattr(ctx, "locator_resolver", None)
    cache = getattr(ctx, "element_cache", None)
    if resolver is None:
        return ActionResult(
            success=False,
            summary="page_action unavailable: locator_resolver not configured. "
            "Use click(index=N) instead.",
        )

    # 混合模式定位：Phase A 常规重试（App + Web 通用）→ 仅 Web 走 Phase B DOM 深扫。
    # scroll_to 的真实滚动（scrollIntoView）由 Phase A 每轮内部处理。
    result, recovery_trace = await _resolve_with_hybrid_recovery(ctx, action, element)
    if not result.success:
        return ActionResult(
            success=False,
            summary=_build_failure_summary(element, recovery_trace),
        )

    x, y = result.x, result.y
    # 重试可能已切换页面，取最新 current_page 解析元素级配置
    page_def = _current_page(ctx)
    el_def = page_def.get("elements", {}).get(element, {}) if page_def else {}
    post_wait = el_def.get("post_action_wait", 0.0) or 0.0

    try:
        if action == "click":
            await ctx.driver.tap(x, y)
            _mark_state_dirty(ctx)
            summary = f"Clicked '{element}' at ({x}, {y})"
        elif action == "type":
            await ctx.driver.tap(x, y)
            await asyncio.sleep(0.2)
            await ctx.driver.input_text(value, clear=True)
            _mark_state_dirty(ctx)
            summary = f"Typed into '{element}' at ({x}, {y})"
        elif action == "scroll_to":
            _mark_state_dirty(ctx)
            summary = f"Scrolled to '{element}' at ({x}, {y})"
        elif action == "wait_for":
            summary = f"Element '{element}' is present at ({x}, {y})"
        elif action == "verify":
            summary = f"Verified '{element}' present at ({x}, {y})"
        else:  # pragma: no cover — already validated
            return ActionResult(success=False, summary=f"Unhandled action '{action}'")
    except Exception as e:  # noqa: BLE001
        return ActionResult(
            success=False, summary=f"page_action '{action}' on '{element}' failed: {e}"
        )

    if post_wait > 0:
        await asyncio.sleep(post_wait)

    # AJAX 刷新补偿：失效指定元素的缓存。
    # 运行时参数优先；未传则回退到页面定义里该元素声明的 invalidate_after。
    targets = invalidate_after
    if targets is None:
        targets = el_def.get("invalidate_after") or []
    if targets and cache is not None:
        viewport = _driver_viewport(ctx.driver)
        for name in targets:
            cache.invalidate_element(name, viewport)

    cache_note = (
        " (cache hit)" if result.cache_hit else f" via {result.strategy_used.name}"
    )
    return ActionResult(success=True, summary=summary + cache_note)


def _driver_viewport(driver) -> dict:
    vp = getattr(driver, "_viewport", None) or {}
    return {"width": vp.get("width", 0), "height": vp.get("height", 0)}
