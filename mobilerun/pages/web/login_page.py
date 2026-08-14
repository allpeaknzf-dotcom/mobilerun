"""手动优化版登录页示例 — 提供最高精度的定位器。

手动 Page Object 是可选的性能优化项，不是使用 PEL 的前置条件。
"""

from mobilerun.pages.base_page import ElementSpec, PageObject


class LoginPage(PageObject):
    """手动定义的登录页对象。"""

    url_patterns = [r"/login", r"/signin", r"/auth"]
    key_element_texts = ["用户名", "密码", "登录"]
    title_patterns = [r"登录", r"Login", r"Sign In"]
    match_threshold = 60
    platform = "web"

    elements = {
        "用户名输入框": ElementSpec(
            name="用户名输入框",
            locators={
                "common": [
                    {"text": "用户名"},
                    {"text": "账号"},
                    {"text": "邮箱"},
                ],
                "web": [
                    {"css": "input[name='username']"},
                    {"css": "input[placeholder*='用户名']"},
                    {"css": "input[placeholder*='邮箱']"},
                    {"css": "input[type='email']"},
                    {"spatial": "the first text input on the page"},
                ],
                "android": [
                    {"id": "com.example:id/et_username"},
                    {"id": "com.example:id/et_email"},
                ],
                "ios": [
                    {"accessibility_id": "usernameField"},
                ],
            },
            required=True,
            post_action_wait=0.3,
        ),
        "密码输入框": ElementSpec(
            name="密码输入框",
            locators={
                "common": [
                    {"text": "密码"},
                ],
                "web": [
                    {"css": "input[type='password']"},
                    {"css": "input[placeholder*='密码']"},
                ],
            },
            required=True,
        ),
        "登录按钮": ElementSpec(
            name="登录按钮",
            locators={
                "common": [
                    {"text": "登录"},
                    {"text": "Login"},
                    {"text": "Sign in"},
                ],
                "web": [
                    {"css": "button[type='submit']"},
                    {"css": "input[type='submit']"},
                    {"spatial": "below:密码输入框"},
                ],
            },
            required=True,
            post_action_wait=1.5,
        ),
    }
