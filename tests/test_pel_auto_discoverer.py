"""Regression tests for PEL auto-discovery key element selection."""

import asyncio
import tempfile
import unittest

from mobilerun.auto.discoverer import PageDiscoverer
from mobilerun.tools.ui.state import UIState


class AutoDiscoverKeyTextsTest(unittest.TestCase):
    def test_auto_discovery_excludes_synthetic_container_text(self):
        """Synthetic roots like WebPage must not become fingerprint keys."""

        class FakeDriver:
            platform = "Web"
            _viewport = {"width": 1280, "height": 720}

            async def evaluate(self, js):
                return []

        elements = [
            {"className": "WebPage", "text": "WebPage", "bounds": "0,0,0,0", "index": 1},
            {"className": "button", "text": "One", "bounds": "8,8,49,29", "index": 2},
            {"className": "button", "text": "Open New", "bounds": "49,8,128,29", "index": 3},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            async def run():
                discoverer = PageDiscoverer(FakeDriver(), UIState(
                    elements=elements,
                    formatted_text="",
                    focused_text="",
                    phone_state={},
                    screen_width=1280,
                    screen_height=720,
                ), yaml_dir=tmp)
                result = await discoverer.discover()
                return result

        page = asyncio.run(run())
        key_texts = page.get("key_element_texts", [])
        self.assertIn("One", key_texts)
        self.assertIn("Open New", key_texts)
        self.assertNotIn("WebPage", key_texts)
        self.assertEqual(len(key_texts), 2)


if __name__ == "__main__":
    unittest.main()
