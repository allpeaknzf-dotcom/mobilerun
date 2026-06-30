"""ActionContext — composed bag of dependencies for action functions.

Replaces the ``tools=tools_instance`` parameter that action functions
previously received.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from mobilerun_core_cli.driver.base import DeviceDriver

    from mobilerun.agent.droid.state import MobileAgentState
    from mobilerun.credential_manager import CredentialManager
    from mobilerun.macro.recorder import MacroRecorder
    from mobilerun.tools.ui.provider import StateProvider
    from mobilerun.tools.ui.state import UIState


class ActionContext:
    """Everything an action function needs to interact with the device."""

    def __init__(
        self,
        driver: "DeviceDriver",
        ui: "Optional[UIState]",
        shared_state: "MobileAgentState",
        state_provider: "StateProvider",
        app_opener_llm=None,
        credential_manager: "Optional[CredentialManager]" = None,
        streaming: bool = False,
        macro_recorder: "Optional[MacroRecorder]" = None,
        # PEL 新增 ↓ —— 页面元素层（缺省 None，未接入时行为完全不变）
        page_registry=None,
        locator_resolver=None,
        element_cache=None,
    ) -> None:
        self.driver = driver
        self.ui = ui  # refreshed each step before tool execution
        self.shared_state = shared_state
        self.state_provider = state_provider
        self.app_opener_llm = app_opener_llm
        self.credential_manager = credential_manager
        self.streaming = streaming
        self.macro_recorder = macro_recorder
        # PEL components — 供 page_action 工具访问
        self.page_registry = page_registry
        self.locator_resolver = locator_resolver
        self.element_cache = element_cache
