"""手动优化版 App 首页示例（Android/iOS）。

App 端没有 URL，主要靠 activity_patterns（Android）/ key_element_texts 匹配。
定位优先 accessibility_id（resource-id），再回退 text。
"""

from mobilerun.pages.base_page import ElementSpec, PageObject


class HomePage(PageObject):
    """手动定义的 App 首页对象。"""

    activity_patterns = [r"\.MainActivity", r"\.HomeActivity"]
    key_element_texts = ["首页", "搜索", "我的"]
    match_threshold = 40
    platform = "android"

    elements = {
        "搜索框": ElementSpec(
            name="搜索框",
            locators={
                "common": [
                    {"text": "搜索"},
                ],
                "android": [
                    {"id": "com.example:id/search_box"},
                    {"id": "com.example:id/et_search"},
                ],
                "ios": [
                    {"accessibility_id": "searchField"},
                ],
            },
            required=True,
            post_action_wait=0.3,
        ),
        "我的入口": ElementSpec(
            name="我的入口",
            locators={
                "common": [
                    {"text": "我的"},
                ],
                "android": [
                    {"id": "com.example:id/tab_mine"},
                ],
                "ios": [
                    {"accessibility_id": "tab_mine"},
                ],
            },
            required=True,
            post_action_wait=0.8,
        ),
    }
