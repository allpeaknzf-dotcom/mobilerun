"""WebStateProvider — builds UIState from WebDriver + DOM extractor."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mobilerun.agent.utils.dom_extractor import normalize_element
from mobilerun.tools.helpers.images import fit_dimensions_to_max_side
from mobilerun.tools.ui.provider import StateProvider
from mobilerun.tools.ui.state import UIState

if TYPE_CHECKING:
    from mobilerun.tools.driver.web import WebDriver

logger = logging.getLogger("mobilerun")


class WebStateProvider(StateProvider):
    """Build UIState from WebDriver + DOM extractor.

    Skips fetch_state_with_retry and tree_filter (Android-only concepts).
    Uses normalize_element() to map DOM fields → formatter format.
    """

    supported = {"element_index", "convert_point"}
    platform = "Web"

    def __init__(
        self,
        driver: WebDriver,
        use_normalized: bool = False,
        vision_enabled: bool = False,
        vision_resize_policy: Any = None,
    ) -> None:
        super().__init__(driver)
        self.tree_formatter = None
        self.tree_filter = None
        self.use_normalized = use_normalized
        self.screenshot_matches_input_coords = True
        self.requires_coordinate_tools = False
        self.resize_model_screenshot = vision_enabled
        self.vision_resize_policy = vision_resize_policy

    async def get_state(self) -> UIState:
        from mobilerun.tools.formatters.indexed_formatter import IndexedFormatter

        if self.tree_formatter is None:
            self.tree_formatter = IndexedFormatter()
            self.tree_formatter.use_normalized = self.use_normalized

        raw = await self.driver.get_ui_tree()
        elements = raw["raw_elements"]
        device_context = raw["device_context"]
        screen_width = device_context["screen_bounds"]["width"]
        screen_height = device_context["screen_bounds"]["height"]

        normalized = [normalize_element(el) for el in elements]

        url = device_context.get("url", "")
        title = device_context.get("title", "")
        phone_state = {
            "currentApp": title,
            "packageName": url,
            "focusedElement": {},
            "isEditable": False,
        }

        self.tree_formatter.screen_width = screen_width
        self.tree_formatter.screen_height = screen_height
        self.tree_formatter.use_normalized = self.use_normalized
        self.tree_formatter.display_scale_x = 1.0
        self.tree_formatter.display_scale_y = 1.0

        # Wrap flat elements in a root node — IndexedFormatter expects a tree structure
        root_node = {"className": "WebPage", "text": "", "children": normalized, "bounds": "0,0,0,0", "boundsInScreen": {"left": 0, "top": 0, "right": 0, "bottom": 0}}

        formatted_text, focused_text, elements_out, _ = self.tree_formatter.format(
            root_node, phone_state
        )

        display_width = None
        display_height = None
        coordinate_scale_x = 1.0
        coordinate_scale_y = 1.0

        if self.resize_model_screenshot and screen_width and screen_height:
            screenshot = await self.driver.screenshot()
            from mobilerun.tools.helpers.images import image_dimensions
            shot_width, shot_height = image_dimensions(screenshot)
            if self.vision_resize_policy is not None:
                display_width, display_height = (
                    self.vision_resize_policy.effective_dims(shot_width, shot_height)
                )
            else:
                display_width, display_height = fit_dimensions_to_max_side(
                    shot_width, shot_height
                )
            coordinate_scale_x = screen_width / display_width
            coordinate_scale_y = screen_height / display_height
            self.resize_model_screenshot = bool(display_width and display_height)
            if display_width and display_height:
                scaled_note = (
                    f" — the {screen_width}x{screen_height} viewport scaled down"
                    if (display_width, display_height) != (screen_width, screen_height)
                    else ""
                )
                formatted_text += (
                    f"\n\nAll coordinates are in a "
                    f"{display_width}x{display_height} coordinate space{scaled_note}. "
                    f"The screenshot carries a labeled coordinate grid for reference."
                )

        return UIState(
            elements=elements_out,
            formatted_text=formatted_text,
            focused_text=focused_text,
            phone_state=phone_state,
            screen_width=screen_width,
            screen_height=screen_height,
            use_normalized=self.use_normalized,
            coordinate_scale_x=coordinate_scale_x,
            coordinate_scale_y=coordinate_scale_y,
            coordinate_contract_active=bool(display_width and display_height),
        )
