"""Migration v8: Add web config section.

Adds a default ``web`` config block with all fields at their defaults.
This enables the new ``--platform web`` mode without requiring users to
manually edit their YAML.
"""

from typing import Any, Dict

VERSION = 8

WEB_DEFAULTS = {
    "headless": True,
    "viewport_width": 1280,
    "viewport_height": 720,
    "device_profile": None,
    "user_agent": None,
    "start_url": "about:blank",
    "browser_type": "chromium",
    "stealth": False,
    "timeout_ms": 30000,
    "locale": "zh-CN",
    "wechat_mock": False,
    "geolocation": None,
    "cookies": [],
    "local_storage": {},
}


def migrate(config: Dict[str, Any]) -> Dict[str, Any]:
    if "web" not in config:
        config["web"] = dict(WEB_DEFAULTS)
    else:
        # Fill in any missing keys with defaults
        web = config["web"]
        for key, default in WEB_DEFAULTS.items():
            if key not in web:
                web[key] = default
        config["web"] = web
    return config
