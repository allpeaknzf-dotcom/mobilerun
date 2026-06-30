"""PEL (Page Element Layer) 回归测试。

覆盖：选择器归一化、坐标缓存（viewport 隔离 / 指纹失效 / per-page 命中计数 /
LRU）、结构指纹的坐标无关性、PageRegistry 多源匹配、YAML 往返 + 用户编辑合并、
LocatorResolver（CSS 命中 + 缓存命中 + 平台过滤 + Web 文本/空间匹配）、
CachedStateProvider（缓存命中 / 强制刷新 / 指纹失效 / 属性透传 / 自动发现）、
page_action 工具与条件注册。

风格对齐仓库现有测试：unittest + asyncio.run，使用 Fake 替身，不依赖真实
浏览器 / 设备。
"""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

import yaml

from mobilerun.agent.utils.page_actions import page_action
from mobilerun.agent.utils.signatures import build_tool_registry
from mobilerun.auto.yaml_generator import YAMLGenerator
from mobilerun.element.cache import ElementCache
from mobilerun.element.locator import Strategy, normalize_selectors
from mobilerun.element.resolver import LocatorResolver
from mobilerun.pages.registry import PageRegistry
from mobilerun.pages.web.login_page import LoginPage
from mobilerun.tools.ui.cached_provider import CachedStateProvider
from mobilerun.tools.ui.state import UIState


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeWebDriver:
    platform = "Web"

    def __init__(self):
        self._viewport = {"width": 1280, "height": 720}
        self._href = "https://x.com/login"
        self._title = "登录页"
        self._count = 3
        self.taps = []
        self.typed = []
        self.scrolled = []
        self.css_result = {
            "x": 640, "y": 400, "bounds": "620,390,660,410",
            "tag": "button", "type": "submit", "text": "登录",
        }

    async def evaluate(self, js):
        if "scrollIntoView" in js:
            self.scrolled.append(js)
            return True
        if "location.href" in js:
            return self._href
        if "document.title" in js:
            return self._title
        if ".length" in js:
            return self._count
        if "includes" in js:  # 关键文本存在性检查
            return ["用户名", "密码"]
        return self.css_result

    async def tap(self, x, y):
        self.taps.append((x, y))

    async def input_text(self, text, clear=False):
        self.typed.append((text, clear))
        return True


def make_web_state():
    return UIState(
        elements=[
            {"index": 0, "className": "input:text", "type": "text", "text": "用户名",
             "bounds": "10,10,200,40",
             "boundsInScreen": {"left": 10, "top": 10, "right": 200, "bottom": 40}},
            {"index": 1, "className": "input:password", "type": "password",
             "text": "密码", "bounds": "10,50,200,80",
             "boundsInScreen": {"left": 10, "top": 50, "right": 200, "bottom": 80}},
        ],
        formatted_text="ft", focused_text="",
        phone_state={"packageName": "https://x.com/login", "currentApp": "登录页"},
        screen_width=1280, screen_height=720,
    )


class FakeInner:
    platform = "Web"
    supported = {"element_index", "convert_point"}
    screenshot_matches_input_coords = True
    resize_model_screenshot = False

    def __init__(self, driver):
        self.driver = driver
        self.calls = 0

    async def get_state(self):
        self.calls += 1
        return make_web_state()


# ---------------------------------------------------------------------------
# locator 归一化
# ---------------------------------------------------------------------------


class NormalizeSelectorsTest(unittest.TestCase):
    def test_short_and_canonical_forms(self):
        out = normalize_selectors([
            {"css": "#btn"},
            {"text": "登录"},
            {"spatial": "below:x"},
            {"id": "com.x:id/b"},
            {"type": Strategy.TEXT_FUZZY, "value": "foo"},
            {"bogus": "x"},  # 无法识别 → 丢弃
        ])
        types = [s["type"] for s in out]
        self.assertEqual(types, [
            Strategy.CSS_SELECTOR, Strategy.TEXT_FUZZY, Strategy.SPATIAL,
            Strategy.ACCESSIBILITY_ID, Strategy.TEXT_FUZZY,
        ])
        self.assertEqual(out[0]["value"], "#btn")

    def test_empty_input(self):
        self.assertEqual(normalize_selectors(None), [])
        self.assertEqual(normalize_selectors([]), [])


# ---------------------------------------------------------------------------
# ElementCache
# ---------------------------------------------------------------------------


class ElementCacheTest(unittest.TestCase):
    def test_get_set_and_viewport_isolation(self):
        c = ElementCache(default_ttl=100)
        c.set_page_fingerprint("fp1")
        vp = {"width": 1280, "height": 720}
        c.set("登录按钮", 100, 200, "CSS_SELECTOR", viewport=vp)
        e = c.get("登录按钮", vp)
        self.assertIsNotNone(e)
        self.assertEqual((e.x, e.y), (100, 200))
        # 不同 viewport 互不污染
        self.assertIsNone(c.get("登录按钮", {"width": 390, "height": 844}))

    def test_fingerprint_switch_clears_old_entries(self):
        c = ElementCache(default_ttl=100)
        c.set_page_fingerprint("fp1")
        vp = {"width": 1280, "height": 720}
        c.set("登录按钮", 1, 2, "TEXT_FUZZY", viewport=vp)
        c.set_page_fingerprint("fp2")
        self.assertIsNone(c.get("登录按钮", vp))

    def test_per_page_hit_counter_and_force_refresh(self):
        c = ElementCache(max_cached_hits=3)
        c.set_page_fingerprint("fpA")
        for _ in range(3):
            c.increment_hit()
        self.assertEqual(c.get_hit_count(), 3)
        self.assertTrue(c.should_force_refresh())
        # 切到另一页面，计数独立
        c.set_page_fingerprint("fpB")
        self.assertEqual(c.get_hit_count(), 0)
        self.assertFalse(c.should_force_refresh())

    def test_lru_eviction(self):
        c = ElementCache(max_entries=2)
        c.set_page_fingerprint("fp")
        vp = {"width": 1, "height": 1}
        c.set("a", 1, 1, "TEXT_FUZZY", viewport=vp)
        c.set("b", 2, 2, "TEXT_FUZZY", viewport=vp)
        c.get("a", vp)  # a 变成最近使用
        c.set("c", 3, 3, "TEXT_FUZZY", viewport=vp)  # 淘汰 b（最久未用）
        self.assertIsNotNone(c.get("a", vp))
        self.assertIsNone(c.get("b", vp))
        self.assertIsNotNone(c.get("c", vp))

    def test_invalidate_element_public_api(self):
        c = ElementCache()
        c.set_page_fingerprint("fp")
        vp = {"width": 1, "height": 1}
        c.set("错误提示", 5, 5, "TEXT_FUZZY", viewport=vp)
        self.assertTrue(c.invalidate_element("错误提示", vp))
        self.assertIsNone(c.get("错误提示", vp))
        self.assertFalse(c.invalidate_element("不存在", vp))

    def test_fingerprint_is_coordinate_invariant(self):
        c = ElementCache()
        els1 = [{"className": "button:submit", "type": "submit", "text": "登录",
                 "boundsInScreen": {"left": 0, "top": 0, "right": 10, "bottom": 10}}]
        els2 = [{"className": "button:submit", "type": "submit", "text": "登录",
                 "boundsInScreen": {"left": 99, "top": 99, "right": 110, "bottom": 110}}]
        self.assertEqual(
            c.compute_fingerprint(els1), c.compute_fingerprint(els2)
        )


# ---------------------------------------------------------------------------
# PageRegistry
# ---------------------------------------------------------------------------


class PageRegistryTest(unittest.TestCase):
    def test_python_page_match(self):
        r = PageRegistry()
        r.register(LoginPage)
        m = r.match({
            "url": "https://x.com/login", "title": "登录",
            "elements": [{"text": "用户名"}, {"text": "密码"}, {"text": "登录"}],
        })
        self.assertIsNotNone(m)
        self.assertEqual(m["source"], "python")
        self.assertEqual(m["score"], 100)
        locs = r.get_element_locators(m["page"], "登录按钮", "web")
        self.assertTrue(any("css" in l or "text" in l for l in locs))

    def test_no_match_returns_none(self):
        r = PageRegistry()
        r.register(LoginPage)
        self.assertIsNone(
            r.match({"url": "https://x.com/home", "title": "首页", "elements": []})
        )

    def test_priority_python_over_yaml(self):
        r = PageRegistry()
        r.register(LoginPage)
        # 注入一个同样能匹配的 auto 页面，python 应优先
        r.add_auto_page({
            "page_id": "auto_login", "url_patterns": ["/login"],
            "key_element_texts": ["登录"], "match_threshold": 20,
            "elements": {},
        })
        m = r.match({"url": "https://x.com/login", "title": "登录",
                     "elements": [{"text": "登录"}]})
        self.assertEqual(m["source"], "python")

    def test_load_python_pages_makes_manual_path_usable(self):
        # 全定制模式：扫描 pages 包注册手写 PageObject 类
        r = PageRegistry()
        n = r.load_python_pages("web")
        self.assertGreaterEqual(n, 1)  # 至少 LoginPage
        m = r.match({
            "url": "https://x.com/login", "title": "登录",
            "elements": [{"text": "用户名"}, {"text": "密码"}, {"text": "登录"}],
        })
        self.assertIsNotNone(m)
        self.assertEqual(m["source"], "python")

    def test_load_python_pages_platform_filter(self):
        r = PageRegistry()
        r.load_python_pages("web")
        # HomePage 是 android，web 过滤后不应命中 android activity
        m = r.match({"url": "", "title": "", "activity": ".MainActivity",
                     "elements": [{"text": "首页"}]})
        self.assertIsNone(m)


# ---------------------------------------------------------------------------
# YAMLGenerator + registry 加载
# ---------------------------------------------------------------------------


class YAMLGeneratorTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_roundtrip_and_user_edit_merge(self):
        g = YAMLGenerator(self.dir)
        data = g.build_yaml_data(
            "auto_test", ["x.com/login"], ["登录"], ["用户名", "密码"],
            "web", "1280x720",
            {"登录按钮": {"detected_selectors": [{"css": "#submit"}], "required": True}},
            "2026-06-17T10:00:00",
        )
        path = g.write("auto_test", data)
        self.assertTrue(os.path.exists(path))

        # 模拟用户编辑：插入自定义选择器 + 新增元素
        with open(path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        loaded["elements"]["登录按钮"]["detected_selectors"].insert(
            0, {"css": "#my-custom"}
        )
        loaded["elements"]["新元素"] = {"detected_selectors": [{"text": "额外"}]}
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(loaded, f, allow_unicode=True)

        # 自动发现重跑 → 用户编辑应保留
        data2 = g.build_yaml_data(
            "auto_test", ["x.com/login"], ["登录"], ["用户名"], "web", "1280x720",
            {"登录按钮": {"detected_selectors": [{"css": "#submit"}], "required": True}},
            "2026-06-17T11:00:00",
        )
        g.write("auto_test", data2)
        with open(path, encoding="utf-8") as f:
            merged = yaml.safe_load(f)
        sels = merged["elements"]["登录按钮"]["detected_selectors"]
        self.assertIn({"css": "#my-custom"}, sels)
        self.assertIn("新元素", merged["elements"])

    def test_registry_loads_yaml(self):
        g = YAMLGenerator(self.dir)
        data = g.build_yaml_data(
            "auto_login", ["x.com/login"], ["登录"], ["用户名"], "web", "1280x720",
            {"用户名输入框": {"detected_selectors": [{"css": "#u"}], "required": True}},
            "2026-06-17T10:00:00",
        )
        g.write("auto_login", data)
        r = PageRegistry()
        self.assertEqual(r.load_yaml_pages(self.dir), 1)
        m = r.match({"url": "https://x.com/login", "title": "登录",
                     "elements": [{"text": "用户名"}]})
        self.assertIsNotNone(m)
        self.assertEqual(m["source"], "yaml")

    def test_malformed_yaml_is_skipped(self):
        bad = os.path.join(self.dir, "broken.yaml")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("elements: [unclosed\n  : : :")
        r = PageRegistry()
        # 不抛异常，返回成功加载数（坏文件被跳过）
        self.assertEqual(r.load_yaml_pages(self.dir), 0)


# ---------------------------------------------------------------------------
# LocatorResolver
# ---------------------------------------------------------------------------


class FakeSP:
    def __init__(self, elements=None):
        self._elements = elements or []

    async def get_state(self):
        return SimpleNamespace(elements=self._elements)


class LocatorResolverTest(unittest.TestCase):
    def test_css_hit_then_cache_hit(self):
        async def run():
            cache = ElementCache()
            cache.set_page_fingerprint("fp")
            drv = FakeWebDriver()
            res = LocatorResolver(drv, FakeSP(), cache)
            r1 = await res.resolve([{"css": "#submit"}], element_name="登录按钮")
            self.assertTrue(r1.success)
            self.assertEqual((r1.x, r1.y), (640, 400))
            self.assertFalse(r1.cache_hit)
            self.assertEqual(r1.strategy_used, Strategy.CSS_SELECTOR)
            r2 = await res.resolve([{"css": "#submit"}], element_name="登录按钮")
            self.assertTrue(r2.cache_hit)
            self.assertEqual((r2.x, r2.y), (640, 400))

        asyncio.run(run())

    def test_css_filtered_on_android(self):
        async def run():
            cache = ElementCache()
            cache.set_page_fingerprint("fp")
            drv = FakeWebDriver()
            drv.platform = "Android"
            res = LocatorResolver(drv, FakeSP(), cache)
            r = await res.resolve([{"css": "#submit"}], element_name="x")
            self.assertFalse(r.success)

        asyncio.run(run())

    def test_web_text_match(self):
        async def run():
            cache = ElementCache()
            cache.set_page_fingerprint("fp")
            drv = FakeWebDriver()
            sp = FakeSP(make_web_state().elements)
            res = LocatorResolver(drv, sp, cache)
            r = await res.resolve([{"text": "用户名"}], element_name="用户名输入框")
            self.assertTrue(r.success)
            # 中心点 = ((10+200)//2, (10+40)//2)
            self.assertEqual((r.x, r.y), (105, 25))

        asyncio.run(run())

    def test_web_spatial_below(self):
        async def run():
            cache = ElementCache()
            cache.set_page_fingerprint("fp")
            drv = FakeWebDriver()
            sp = FakeSP(make_web_state().elements)
            res = LocatorResolver(drv, sp, cache)
            # 密码在用户名下方
            r = await res.resolve(
                [{"spatial": "below:用户名"}], element_name="密码输入框"
            )
            self.assertTrue(r.success)
            self.assertEqual((r.x, r.y), (105, 65))

        asyncio.run(run())

    def test_scroll_into_view_web(self):
        async def run():
            cache = ElementCache()
            cache.set_page_fingerprint("fp")
            drv = FakeWebDriver()
            res = LocatorResolver(drv, FakeSP(), cache)
            ok = await res.scroll_into_view([{"css": "#far"}])
            self.assertTrue(ok)
            # 纯 text 选择器不触发滚动
            self.assertFalse(await res.scroll_into_view([{"text": "登录"}]))

        asyncio.run(run())


# ---------------------------------------------------------------------------
# CachedStateProvider
# ---------------------------------------------------------------------------


class CachedStateProviderTest(unittest.TestCase):
    def setUp(self):
        # discover() 会写 .mobilerun/，切到临时目录避免污染仓库
        self._cwd = os.getcwd()
        os.chdir(tempfile.mkdtemp())

    def tearDown(self):
        os.chdir(self._cwd)

    def _build(self, max_hits=5):
        drv = FakeWebDriver()
        inner = FakeInner(drv)
        cache = ElementCache(max_cached_hits=max_hits)
        reg = PageRegistry()
        resolver = LocatorResolver(drv, None, cache)
        csp = CachedStateProvider(inner, cache, reg, resolver)
        resolver.state_provider = csp
        return drv, inner, cache, csp

    def test_attribute_passthrough(self):
        _, inner, _, csp = self._build()
        self.assertEqual(csp.supported, inner.supported)
        self.assertTrue(csp.screenshot_matches_input_coords)
        self.assertIs(csp.driver, inner.driver)

    def test_probe_then_cache_hit(self):
        async def run():
            _, inner, _, csp = self._build()
            s1 = await csp.get_state()
            self.assertEqual(inner.calls, 1)
            self.assertIsNotNone(csp.current_page)  # 自动发现
            s2 = await csp.get_state()
            self.assertEqual(inner.calls, 1)  # 缓存命中，未重探
            self.assertIs(s2, s1)

        asyncio.run(run())

    def test_force_refresh_after_max_hits(self):
        async def run():
            _, inner, cache, csp = self._build(max_hits=3)
            await csp.get_state()           # full
            for _ in range(3):              # 3 次命中
                await csp.get_state()
            self.assertEqual(inner.calls, 1)
            self.assertEqual(cache.get_hit_count(), 3)
            await csp.get_state()           # 达上限 → 强制刷新
            self.assertEqual(inner.calls, 2)

        asyncio.run(run())

    def test_fingerprint_invalidation_on_title_change(self):
        async def run():
            drv, inner, _, csp = self._build()
            await csp.get_state()
            drv._title = "新页面"
            await csp.get_state()
            self.assertEqual(inner.calls, 2)

        asyncio.run(run())


# ---------------------------------------------------------------------------
# page_action 工具 + 条件注册
# ---------------------------------------------------------------------------


class PageActionToolTest(unittest.TestCase):
    def test_conditional_registration(self):
        async def run():
            reg_off, _ = await build_tool_registry(platform="web", pel_enabled=False)
            self.assertNotIn("page_action", reg_off.tools)
            reg_on, names_on = await build_tool_registry(
                platform="web", pel_enabled=True
            )
            self.assertIn("page_action", reg_on.tools)
            self.assertIn("page_action", names_on)

        asyncio.run(run())

    def _ctx(self, with_resolver=True):
        drv = FakeWebDriver()
        cache = ElementCache()
        cache.set_page_fingerprint("fp")
        sp = SimpleNamespace(
            current_page={
                "elements": {
                    "登录按钮": {
                        "detected_selectors": [{"css": "#submit"}],
                        "post_action_wait": 0,
                    }
                }
            },
        )

        async def _get_state():
            return SimpleNamespace(elements=[])

        sp.get_state = _get_state
        res = LocatorResolver(drv, sp, cache) if with_resolver else None
        return SimpleNamespace(
            driver=drv, state_provider=sp,
            locator_resolver=res, element_cache=cache,
        )

    def test_click(self):
        ctx = self._ctx()
        r = asyncio.run(page_action("click", element="登录按钮", ctx=ctx))
        self.assertTrue(r.success)
        self.assertEqual(ctx.driver.taps, [(640, 400)])

    def test_type(self):
        ctx = self._ctx()
        r = asyncio.run(
            page_action("type", element="登录按钮", value="hello", ctx=ctx)
        )
        self.assertTrue(r.success)
        self.assertEqual(ctx.driver.typed, [("hello", True)])

    def test_graceful_degrade_without_resolver(self):
        ctx = self._ctx(with_resolver=False)
        r = asyncio.run(page_action("click", element="x", ctx=ctx))
        self.assertFalse(r.success)
        self.assertIn("click(index", r.summary)

    def test_invalid_action(self):
        ctx = self._ctx()
        r = asyncio.run(page_action("frobnicate", element="x", ctx=ctx))
        self.assertFalse(r.success)

    def test_scroll_to_triggers_scroll(self):
        ctx = self._ctx()
        r = asyncio.run(page_action("scroll_to", element="登录按钮", ctx=ctx))
        self.assertTrue(r.success)
        self.assertTrue(
            any("scrollIntoView" in js for js in ctx.driver.scrolled),
            "scroll_to should call scrollIntoView",
        )

    def test_element_level_invalidate_after(self):
        # 页面定义里声明了元素级 invalidate_after，未传运行时参数时应生效
        ctx = self._ctx()
        ctx.state_provider.current_page["elements"]["登录按钮"][
            "invalidate_after"
        ] = ["错误提示"]
        vp = {"width": 1280, "height": 720}
        ctx.element_cache.set("错误提示", 1, 1, "TEXT_FUZZY", viewport=vp)
        asyncio.run(page_action("click", element="登录按钮", ctx=ctx))
        self.assertIsNone(ctx.element_cache.get("错误提示", vp))


# ---------------------------------------------------------------------------
# 混合模式定位恢复（Phase A 常规重试 + Phase B DOM 深度扫描）
# ---------------------------------------------------------------------------


from mobilerun.element.resolver import (  # noqa: E402
    expand_synonyms,
    is_input_like,
    normalize_keyword,
    score_dom_candidate,
)


def _btn(**over):
    """构造一个 DOM 深扫候选（带合理默认值）。"""
    base = {
        "tag": "button", "type": "submit", "role": "button", "id": "loginBtn",
        "className": "btn btn-primary login", "name": "login", "text": "登录",
        "placeholder": "", "title": "", "ariaLabel": "登录", "href": "",
        "contentEditable": False, "disabled": False, "visible": True,
        "inViewport": True,
        "rect": {"left": 100, "top": 420, "right": 240, "bottom": 464,
                 "width": 140, "height": 44},
        "center": {"x": 170, "y": 442}, "snippet": "<button>登录</button>",
        "source": "document",
    }
    base.update(over)
    return base


class DomScanScoringTest(unittest.TestCase):
    def test_normalize_keyword_strips_suffix(self):
        self.assertEqual(normalize_keyword("登录按钮"), "登录")
        self.assertEqual(normalize_keyword("用户名输入框"), "用户名")
        self.assertEqual(normalize_keyword("login button"), "login")
        # 仅是后缀本身时不剥空
        self.assertEqual(normalize_keyword("按钮"), "按钮")

    def test_expand_synonyms(self):
        syn = expand_synonyms("登录")
        self.assertIn("login", syn)
        self.assertIn("sign in", syn)

    def test_score_prefers_exact_visible_button(self):
        s = score_dom_candidate(_btn(), "登录", expand_synonyms("登录"), "click")
        self.assertGreaterEqual(s, 100)

    def test_disabled_is_heavily_penalized(self):
        ok = score_dom_candidate(_btn(), "登录", expand_synonyms("登录"), "click")
        dis = score_dom_candidate(
            _btn(disabled=True), "登录", expand_synonyms("登录"), "click"
        )
        self.assertLess(dis, ok)

    def test_type_action_penalizes_non_input(self):
        # 语义命中「用户名」但是个 button：type 动作应显著降分（最终由
        # resolve_via_dom_deep_scan 的 is_input_like 门禁兜底拒绝）。
        btn = _btn(text="用户名", ariaLabel="用户名", tag="button")
        s_type = score_dom_candidate(btn, "用户名", expand_synonyms("用户名"), "type")
        s_click = score_dom_candidate(btn, "用户名", expand_synonyms("用户名"), "click")
        self.assertLess(s_type, s_click - 100)

    def test_is_input_like(self):
        self.assertTrue(is_input_like({"tag": "input", "type": "text"}))
        self.assertTrue(is_input_like({"tag": "textarea"}))
        self.assertTrue(is_input_like({"contentEditable": True}))
        self.assertTrue(is_input_like({"role": "searchbox"}))
        self.assertFalse(is_input_like({"tag": "input", "type": "submit"}))
        self.assertFalse(is_input_like({"tag": "button"}))


class DeepScanDriver:
    """可控的 Web driver 替身：CSS 永远失败，深扫返回可配置候选。"""

    platform = "Web"

    def __init__(self, candidates=None, raise_on_scan=False, timeout_on_scan=False,
                 shadow_roots_scanned=0):
        self._viewport = {"width": 1280, "height": 720}
        self._candidates = candidates if candidates is not None else [_btn()]
        self._raise = raise_on_scan
        self._timeout = timeout_on_scan
        self._shadow_roots = shadow_roots_scanned
        self.taps = []
        self.typed = []

    async def evaluate(self, js):
        if "scrollIntoView" in js:
            return False
        if "location.href" in js:
            return "https://x.com/login"
        if "document.title" in js:
            return "登录页"
        if "DEEP" in js or "collectFrom" in js or "deep_scan_scope" in js:
            if self._timeout:
                await asyncio.sleep(5)
            if self._raise:
                raise RuntimeError("injection blocked")
            scope = ("top_document_and_open_shadow" if self._shadow_roots > 0
                     else "top_document_only")
            return {
                "candidates": self._candidates,
                "total_seen": len(self._candidates),
                "truncated": False,
                "iframe_detected": False,
                "cross_origin_iframe": False,
                "shadow_roots_scanned": self._shadow_roots,
                "deep_scan_scope": scope,
            }
        # 常规 CSS 查询：永远未命中（触发 Phase A 失败）
        return None

    async def tap(self, x, y):
        self.taps.append((x, y))

    async def input_text(self, text, clear=False):
        self.typed.append((text, clear))
        return True


class ResolveViaDeepScanTest(unittest.TestCase):
    def _resolver(self, **kw):
        cache = ElementCache()
        cache.set_page_fingerprint("fp")
        drv = DeepScanDriver(**kw)
        return LocatorResolver(drv, FakeSP(), cache), cache

    def test_click_matches_high_score(self):
        async def run():
            res, _ = self._resolver(candidates=[_btn()])
            r = await res.resolve_via_dom_deep_scan(
                element_name="登录按钮", action="click"
            )
            self.assertTrue(r.success)
            self.assertEqual(r.strategy_used, Strategy.DOM_DEEP_SCAN)
            self.assertEqual((r.x, r.y), (170, 442))
        asyncio.run(run())

    def test_disabled_top1_rejected(self):
        async def run():
            res, _ = self._resolver(candidates=[_btn(disabled=True)])
            r = await res.resolve_via_dom_deep_scan(
                element_name="登录按钮", action="click"
            )
            self.assertFalse(r.success)
            self.assertEqual(r.diagnostics["reason"], "top1_disabled")
        asyncio.run(run())

    def test_ambiguous_top1_top2(self):
        async def run():
            # 两个几乎一样的「登录」按钮 → gap 不足
            res, _ = self._resolver(
                candidates=[_btn(id="a"), _btn(id="b", center={"x": 9, "y": 9})]
            )
            r = await res.resolve_via_dom_deep_scan(
                element_name="登录", action="click"
            )
            self.assertFalse(r.success)
            self.assertEqual(r.diagnostics["reason"], "ambiguous")
        asyncio.run(run())

    def test_type_rejects_non_input(self):
        async def run():
            res, _ = self._resolver(
                candidates=[_btn(text="用户名", ariaLabel="用户名", tag="button")]
            )
            r = await res.resolve_via_dom_deep_scan(
                element_name="用户名输入框", action="type"
            )
            self.assertFalse(r.success)
            self.assertIn(r.diagnostics["reason"],
                          ("not_input_like", "score_below_threshold"))
        asyncio.run(run())

    def test_type_matches_input(self):
        async def run():
            inp = _btn(tag="input", type="text", text="", ariaLabel="用户名",
                       id="username", className="form-input")
            res, _ = self._resolver(candidates=[inp])
            r = await res.resolve_via_dom_deep_scan(
                element_name="用户名输入框", action="type"
            )
            self.assertTrue(r.success)
        asyncio.run(run())

    def test_timeout_returns_unavailable(self):
        async def run():
            res, _ = self._resolver(timeout_on_scan=True)
            cands, meta = await res.deep_scan_dom_candidates(timeout=0.05)
            self.assertEqual(cands, [])
            self.assertEqual(meta["reason"], "deep_scan_timeout")
        asyncio.run(run())

    def test_injection_blocked_returns_unavailable(self):
        async def run():
            res, _ = self._resolver(raise_on_scan=True)
            r = await res.resolve_via_dom_deep_scan(
                element_name="登录按钮", action="click"
            )
            self.assertFalse(r.success)
            self.assertEqual(r.diagnostics["reason"], "deep_scan_injection_blocked")
        asyncio.run(run())

    def test_short_ttl_cache_written(self):
        async def run():
            res, cache = self._resolver(candidates=[_btn()])
            r = await res.resolve_via_dom_deep_scan(
                element_name="登录按钮", action="click", cache_ttl=5.0
            )
            self.assertTrue(r.success)
            self.assertTrue(r.diagnostics.get("cached_written"))
            entry = cache.get("登录按钮", {"width": 1280, "height": 720})
            self.assertIsNotNone(entry)
            self.assertLessEqual(entry.ttl, 5.0)
        asyncio.run(run())

    def test_native_platform_skips_scan(self):
        async def run():
            cache = ElementCache()
            cache.set_page_fingerprint("fp")
            drv = DeepScanDriver()
            drv.platform = "Android"
            res = LocatorResolver(drv, FakeSP(), cache)
            cands, meta = await res.deep_scan_dom_candidates()
            self.assertEqual(cands, [])
            self.assertEqual(meta["reason"], "not_supported_on_native")
        asyncio.run(run())

    def test_scope_meta_passthrough_with_shadow(self):
        """Python 侧透传：JS 报告扫过 shadow 时 meta/diag 必须同步反映，
        不再写死 top_document_only（不依赖浏览器，托底集成测试被 skip 的环境）。"""
        async def run():
            res, _ = self._resolver(candidates=[_btn()], shadow_roots_scanned=2)
            cands, meta = await res.deep_scan_dom_candidates()
            self.assertEqual(meta["deep_scan_scope"], "top_document_and_open_shadow")
            self.assertEqual(meta["shadow_roots_scanned"], 2)
            r = await res.resolve_via_dom_deep_scan(
                element_name="登录按钮", action="click"
            )
            self.assertTrue(r.success)
            self.assertEqual(
                r.diagnostics["deep_scan_scope"], "top_document_and_open_shadow"
            )
            self.assertEqual(r.diagnostics["shadow_roots_scanned"], 2)
        asyncio.run(run())

    def test_scope_meta_top_document_only_without_shadow(self):
        async def run():
            res, _ = self._resolver(candidates=[_btn()], shadow_roots_scanned=0)
            _, meta = await res.deep_scan_dom_candidates()
            self.assertEqual(meta["deep_scan_scope"], "top_document_only")
            self.assertEqual(meta["shadow_roots_scanned"], 0)
        asyncio.run(run())


class HybridRecoveryTest(unittest.TestCase):
    """Phase A + Phase B 端到端：通过 page_action 验证「一次失败即终止」已修复。"""

    def _ctx(self, drv, page_def=None):
        cache = ElementCache()
        cache.set_page_fingerprint("fp")
        current = page_def if page_def is not None else {
            "elements": {
                "登录按钮": {"detected_selectors": [{"css": "#submit"}],
                            "post_action_wait": 0}
            }
        }
        sp = SimpleNamespace(current_page=current)

        async def _get_state():
            return SimpleNamespace(elements=[])

        async def _refresh_state(force_full=False):
            return SimpleNamespace(elements=[])

        sp.get_state = _get_state
        sp.refresh_state = _refresh_state
        res = LocatorResolver(drv, sp, cache)
        return SimpleNamespace(
            driver=drv, state_provider=sp,
            locator_resolver=res, element_cache=cache,
        )

    def test_web_phase_b_recovers_after_phase_a_fails(self):
        # CSS 永远失败（Phase A 4 轮全挂）→ Phase B 深扫命中并真实点击
        drv = DeepScanDriver(candidates=[_btn()])
        ctx = self._ctx(drv)
        r = asyncio.run(page_action("click", element="登录按钮", ctx=ctx))
        self.assertTrue(r.success, r.summary)
        self.assertEqual(drv.taps, [(170, 442)])

    def test_final_failure_summary_is_structured(self):
        # 深扫只有 disabled 候选 → 整体失败，但摘要必须结构化可读
        drv = DeepScanDriver(candidates=[_btn(disabled=True)])
        ctx = self._ctx(drv)
        r = asyncio.run(page_action("click", element="登录按钮", ctx=ctx))
        self.assertFalse(r.success)
        self.assertIn("standard attempts=4", r.summary)
        self.assertIn("deep_scan=executed", r.summary)
        self.assertIn("top1_disabled", r.summary)

    def test_scroll_to_does_not_trigger_deep_scan(self):
        drv = DeepScanDriver(candidates=[_btn()])
        ctx = self._ctx(drv)
        r = asyncio.run(page_action("scroll_to", element="登录按钮", ctx=ctx))
        # scroll_to 不接深扫：CSS 失败即最终失败，摘要标记 skipped
        self.assertFalse(r.success)
        self.assertIn("deep_scan=skipped", r.summary)

    def test_native_does_not_enter_phase_b(self):
        drv = DeepScanDriver(candidates=[_btn()])
        drv.platform = "Android"
        ctx = self._ctx(drv)
        r = asyncio.run(page_action("click", element="登录按钮", ctx=ctx))
        self.assertFalse(r.success)
        self.assertIn("not_supported_on_native", r.summary)

    def test_each_retry_rereads_current_page_and_selectors(self):
        """压测方案核心约束：每轮重试都重新读取 current_page + 重算 selectors。

        构造「页面延迟出现」场景（Android，无 Phase B 干扰）：
        - 第 1 轮：current_page=None → 走 fallback_text_fuzzy，provider 返回空
          元素 → 失败；
        - refresh_state 被调用后才把 current_page 设为含目标元素的定义，并让
          provider 吐出含该元素的 UIState；
        - 第 2 轮：重读到新 current_page + 重算 selectors → text 命中成功。

        若实现复用首轮 selectors（不重读），第 2 轮仍是 fallback 且 provider
        早期为空，必然失败 —— 本测试即可捕捉该回归。
        """
        async def run():
            state = {"refreshed": False}
            target_el = {
                "index": 7, "className": "btn", "type": "submit", "text": "登录",
                "boundsInScreen": {"left": 100, "top": 400,
                                   "right": 240, "bottom": 444},
            }

            class _Drv:
                platform = "Android"

                def __init__(self):
                    self._viewport = {"width": 390, "height": 844}
                    self.taps = []

                async def tap(self, x, y):
                    self.taps.append((x, y))

                async def input_text(self, text, clear=False):
                    return True

            drv = _Drv()
            cache = ElementCache()
            cache.set_page_fingerprint("fp")
            sp = SimpleNamespace(current_page=None)

            async def _get_state():
                # 刷新前页面尚未稳定（无目标元素）；刷新后才出现
                els = [target_el] if state["refreshed"] else []
                return SimpleNamespace(elements=els)

            async def _refresh_state(force_full=False):
                state["refreshed"] = True
                # 页面识别成功：current_page 现在带出目标元素的 selectors
                sp.current_page = {
                    "name": "LoginPage",
                    "elements": {
                        "登录按钮": {
                            "detected_selectors": [{"text_exact": "登录"}],
                            "post_action_wait": 0,
                        }
                    },
                }
                return await _get_state()

            sp.get_state = _get_state
            sp.refresh_state = _refresh_state
            res = LocatorResolver(drv, sp, cache)
            ctx = SimpleNamespace(
                driver=drv, state_provider=sp,
                locator_resolver=res, element_cache=cache,
            )

            r = await page_action("click", element="登录按钮", ctx=ctx)
            self.assertTrue(r.success, r.summary)
            # 命中目标元素中心点 ((100+240)//2, (400+444)//2)
            self.assertEqual(drv.taps, [(170, 422)])

        asyncio.run(run())


class RefreshStateTest(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        os.chdir(tempfile.mkdtemp())

    def tearDown(self):
        os.chdir(self._cwd)

    def test_force_full_bypasses_cache_fast_path(self):
        async def run():
            drv = FakeWebDriver()
            inner = FakeInner(drv)
            cache = ElementCache(max_cached_hits=5)
            reg = PageRegistry()
            resolver = LocatorResolver(drv, None, cache)
            csp = CachedStateProvider(inner, cache, reg, resolver)
            resolver.state_provider = csp

            await csp.get_state()             # full probe, calls=1
            await csp.get_state()             # cache hit, calls=1
            self.assertEqual(inner.calls, 1)
            await csp.refresh_state(force_full=True)  # 强制完整探测 → calls=2
            self.assertEqual(inner.calls, 2)

        asyncio.run(run())

    def test_attach_state_provider_repoints_resolver_to_wrapper(self):
        """复刻生产装配顺序（评审 #1）：resolver 先用 inner 构造，包装完成后
        必须 attach 到 CachedStateProvider，否则 text/spatial 绕开 PEL 缓存且
        与 Phase A 刷新的 provider 不同源。"""
        async def run():
            drv = FakeWebDriver()
            inner = FakeInner(drv)
            cache = ElementCache(max_cached_hits=5)
            reg = PageRegistry()
            # 装配顺序：resolver 先拿 inner（此时 csp 还不存在）
            resolver = LocatorResolver(drv, inner, cache)
            self.assertIs(resolver.state_provider, inner)
            csp = CachedStateProvider(inner, cache, reg, resolver)
            # 关键修复：回指到包装后 provider
            resolver.attach_state_provider(csp)
            self.assertIs(resolver.state_provider, csp)
            # resolver 的 text 路径现在经由 csp（带缓存层）取状态
            r = await resolver.resolve([{"text": "用户名"}], element_name="用户名框")
            self.assertTrue(r.success)
            # 走的是 csp.get_state → 完整探测一次
            self.assertEqual(inner.calls, 1)

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Phase B DOM 深扫 —— 真实浏览器集成测试（open shadow root 覆盖）
#
# 纯 Python fake driver 直接返回字典，无法验证 DEEP_DOM_SCAN_JS 的 DOM 遍历
# 逻辑。shadow host（非交互宿主节点）的扫描覆盖只能在真实 JS 引擎里验证，
# 因此这里用 playwright + Chromium 跑一次端到端；浏览器不可用时整组 skip，
# 保持「CI 无浏览器也能跑」的约定。
# ---------------------------------------------------------------------------


def _chromium_available() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except Exception:
        return False

    async def _probe():
        from playwright.async_api import async_playwright
        try:
            async with async_playwright() as p:
                b = await p.chromium.launch()
                await b.close()
            return True
        except Exception:
            return False

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


@unittest.skipUnless(_chromium_available(), "Chromium/playwright not available")
class DeepScanShadowDomBrowserTest(unittest.TestCase):
    """真实浏览器验证：交互元素藏在非交互 shadow host 内时仍被深扫覆盖。"""

    SHADOW_HTML = """
    <!DOCTYPE html><html><body>
    <button id="top">顶层按钮</button>
    <login-form id="host"></login-form>
    <script>
      const sr = document.getElementById('host').attachShadow({mode:'open'});
      sr.innerHTML =
        '<button id="inner">登录</button><input id="u" placeholder="用户名">';
      // 再嵌一层 shadow，验证递归
      const nested = document.createElement('x-dialog');
      sr.appendChild(nested);
      const sr2 = nested.attachShadow({mode:'open'});
      sr2.innerHTML = '<button>确认</button>';
    </script>
    </body></html>
    """

    def _scan(self, html):
        import json as _json

        from mobilerun.element.resolver import (
            DEEP_DOM_SCAN_JS,
            INTERACTIVE_SELECTOR,
        )

        async def run():
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                b = await p.chromium.launch()
                page = await b.new_page()
                await page.set_content(html)
                call = (
                    f"({DEEP_DOM_SCAN_JS})("
                    f"{_json.dumps(INTERACTIVE_SELECTOR)}, 200)"
                )
                res = await page.evaluate(call)
                await b.close()
                return res

        return asyncio.run(run())

    def test_interactive_inside_non_interactive_shadow_host_is_scanned(self):
        res = self._scan(self.SHADOW_HTML)
        cands = res["candidates"]
        shadow = [c for c in cands if c["source"] == "shadow"]
        texts = {(c.get("text") or c.get("placeholder")) for c in cands}
        # 顶层按钮（document）+ shadow 内 登录/用户名/确认 都应被发现
        self.assertIn("顶层按钮", texts)
        self.assertIn("登录", texts)
        self.assertIn("用户名", texts)
        self.assertIn("确认", texts)  # 嵌套 shadow 递归覆盖
        self.assertGreaterEqual(len(shadow), 3)
        # 诊断字段必须反映「实际扫了 shadow」，而非写死 top_document_only
        self.assertEqual(res["deep_scan_scope"], "top_document_and_open_shadow")
        self.assertGreaterEqual(res["shadow_roots_scanned"], 2)

    def test_plain_page_scope_is_top_document_only(self):
        html = (
            "<!DOCTYPE html><html><body>"
            "<button>登录</button><input placeholder='用户名'>"
            "</body></html>"
        )
        res = self._scan(html)
        # 无 shadow host 时 scope 不应误报扫过 shadow
        self.assertEqual(res["deep_scan_scope"], "top_document_only")
        self.assertEqual(res["shadow_roots_scanned"], 0)

    def test_resolve_via_deep_scan_hits_shadow_button(self):
        """端到端：通过 resolve_via_dom_deep_scan 命中 shadow 内的「登录」。"""
        from mobilerun.element.cache import ElementCache as _Cache
        from mobilerun.element.resolver import (
            DEEP_DOM_SCAN_JS,
            INTERACTIVE_SELECTOR,
            LocatorResolver as _Resolver,
        )

        html = self.SHADOW_HTML

        async def run():
            import json as _json

            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                b = await p.chromium.launch()
                page = await b.new_page()
                await page.set_content(html)

                class _PWDriver:
                    platform = "Web"

                    def __init__(self):
                        self._viewport = {"width": 1280, "height": 720}

                    async def evaluate(self, js):
                        return await page.evaluate(js)

                drv = _PWDriver()
                cache = _Cache()
                cache.set_page_fingerprint("fp")
                res = _Resolver(drv, FakeSP(), cache)
                result = await res.resolve_via_dom_deep_scan(
                    element_name="登录按钮", action="click"
                )
                await b.close()
                return result

        result = asyncio.run(run())
        self.assertTrue(result.success, getattr(result, "diagnostics", None))
        self.assertEqual(result.diagnostics["source"], "shadow")


if __name__ == "__main__":
    unittest.main()