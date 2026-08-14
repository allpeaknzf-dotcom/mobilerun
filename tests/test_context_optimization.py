"""Phase 1 Token Optimization — unit tests.

Covers:
- 改动 1: 工具定义按需发送 (tool definitions on-demand)
- 改动 2: device_state 变化检测 (change detection)
- 改动 3: 删除 previous_device_state (remove duplicate injection)
"""

import unittest
from types import SimpleNamespace

from llama_index.core.base.llms.types import ChatMessage

from mobilerun.agent.fast_agent.fast_agent import FastAgent
from mobilerun.agent.manager.manager_agent import ManagerAgent
from mobilerun.agent.manager.stateless_manager_agent import StatelessManagerAgent
from mobilerun.agent.utils.context_hasher import state_hash
from mobilerun.config_manager.config_manager import AgentConfig, MobileConfig

# ======================================================================
# 改动 2: context_hasher — state_hash
# ======================================================================


class StateHashTest(unittest.TestCase):
    def test_same_input_produces_same_hash(self):
        text = "<device_state>\nUI elements: [button, textfield]\n</device_state>"
        self.assertEqual(state_hash(text), state_hash(text))

    def test_different_input_produces_different_hash(self):
        a = "<device_state>\nUI elements: [button]\n</device_state>"
        b = "<device_state>\nUI elements: [button, textfield]\n</device_state>"
        self.assertNotEqual(state_hash(a), state_hash(b))

    def test_hash_is_stable_across_calls(self):
        """Multiple calls with the same input return the same hash."""
        text = "formatted device state content"
        first = state_hash(text)
        for _ in range(10):
            self.assertEqual(first, state_hash(text))

    def test_hash_is_12_chars(self):
        result = state_hash("any text")
        self.assertEqual(len(result), 12)

    def test_empty_string_does_not_raise(self):
        """Empty string should hash without error."""
        result = state_hash("")
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 12)


# ======================================================================
# 改动 1: 工具定义按需发送 — configuration flag
# ======================================================================


class ToolDefinitionsConfigTest(unittest.TestCase):
    def test_default_is_false(self):
        """optimize_tool_definitions 默认 false，兼容旧行为。"""
        cfg = AgentConfig()
        self.assertFalse(cfg.optimize_tool_definitions)

    def test_from_dict_parses_true(self):
        cfg = MobileConfig.from_dict({"agent": {"optimize_tool_definitions": True}})
        self.assertTrue(cfg.agent.optimize_tool_definitions)

    def test_from_dict_parses_false(self):
        cfg = MobileConfig.from_dict({"agent": {"optimize_tool_definitions": False}})
        self.assertFalse(cfg.agent.optimize_tool_definitions)

    def test_from_dict_defaults_to_false_when_absent(self):
        cfg = MobileConfig.from_dict({"agent": {"max_steps": 20}})
        self.assertFalse(cfg.agent.optimize_tool_definitions)


# ======================================================================
# 改动 3: stateless_manager — previous_state 兼容
# ======================================================================


class StatelessManagerPreviousStateTest(unittest.TestCase):
    """Verify that _build_prompt uses getattr fallback for previous_state."""

    def test_getattr_returns_empty_string_when_attr_missing(self):
        """Simulate shared_state without previous_formatted_device_state."""
        result = getattr(object(), "previous_formatted_device_state", "")
        self.assertEqual(result, "")

    def test_getattr_returns_value_when_attr_present(self):
        """Simulate shared_state with the attribute set."""

        class FakeState:
            previous_formatted_device_state = "old state content"

        result = getattr(FakeState(), "previous_formatted_device_state", "")
        self.assertEqual(result, "old state content")


class StatelessManagerPromptTest(unittest.IsolatedAsyncioTestCase):
    async def test_build_prompt_handles_missing_previous_state(self):
        agent = object.__new__(StatelessManagerAgent)
        agent.shared_state = SimpleNamespace(
            instruction="Inspect",
            platform="Android",
            device_date="",
            previous_plan="",
            agent_memory="",
            last_thought="",
            progress_summary="",
            action_history=[],
            summary_history=[],
            action_outcomes=[],
            error_descriptions=[],
            formatted_device_state="Current UI",
        )
        agent.prompt_resolver = SimpleNamespace(
            get_prompt=lambda name: "{{ previous_state }}|{{ current_state }}"
        )

        prompt = await StatelessManagerAgent._build_prompt(agent)

        self.assertEqual(prompt, "|Current UI")


# ======================================================================
# 改动 2: device_state change detection (logic simulation)
# ======================================================================


class DeviceStateChangeDetectionTest(unittest.TestCase):
    """Simulate the hash-based injection logic from FastAgent & ManagerAgent."""

    def test_unchanged_state_injects_placeholder(self):
        """When hash matches last injected, inject <device_state_unchanged/>."""
        last_hash: str = ""
        states = [
            "<device_state>\npage A\n</device_state>",  # first injection
            "<device_state>\npage A\n</device_state>",  # same → placeholder
            "<device_state>\npage A\n</device_state>",  # same → placeholder
        ]

        full_count = 0
        placeholder_count = 0

        for state_text in states:
            h = state_hash(state_text)
            if h != last_hash:
                full_count += 1
                last_hash = h
            else:
                placeholder_count += 1

        self.assertEqual(full_count, 1, "only the first unique state should be full")
        self.assertEqual(placeholder_count, 2, "the two repeats should be placeholders")

    def test_changed_state_injects_full_text(self):
        """When hash differs, inject full <device_state> text."""
        last_hash: str = ""
        states = [
            "<device_state>\npage A\n</device_state>",
            "<device_state>\npage B\n</device_state>",
            "<device_state>\npage C\n</device_state>",
        ]

        full_count = 0
        placeholder_count = 0

        for state_text in states:
            h = state_hash(state_text)
            if h != last_hash:
                full_count += 1
                last_hash = h
            else:
                placeholder_count += 1

        self.assertEqual(full_count, 3, "all three different states should be full")
        self.assertEqual(placeholder_count, 0, "no placeholders expected")

    def test_wait_action_keeps_state_unchanged(self):
        """Simulate a wait() action — state unchanged → placeholder."""
        last_hash: str = ""
        states = [
            "initial state",
            "initial state",  # wait() — still the same
            "initial state",  # another wait()
        ]

        full_count = 0
        placeholder_count = 0

        for state_text in states:
            h = state_hash(state_text)
            if h != last_hash:
                full_count += 1
                last_hash = h
            else:
                placeholder_count += 1

        self.assertEqual(full_count, 1)
        self.assertEqual(placeholder_count, 2)


class DeviceStateInjectionBehaviorTest(unittest.TestCase):
    def test_fast_agent_emits_full_then_unchanged_then_full(self):
        agent = object.__new__(FastAgent)
        agent._last_injected_state_hash = ""

        first = agent._device_state_context("page A")
        repeated = agent._device_state_context("page A")
        changed = agent._device_state_context("page B")

        self.assertIn("<device_state>\npage A", first)
        self.assertEqual(repeated, "\n<device_state_unchanged/>\n")
        self.assertIn("<device_state>\npage B", changed)

    def test_manager_builds_messages_without_previous_state_duplication(self):
        agent = object.__new__(ManagerAgent)
        agent._last_injected_state_hash = ""
        agent.vision = False
        agent.shared_state = SimpleNamespace(
            message_history=[
                ChatMessage(role="user", content="first"),
                ChatMessage(role="assistant", content="response"),
                ChatMessage(role="user", content="latest"),
            ],
            agent_memory="",
            formatted_device_state="Current UI",
            previous_formatted_device_state="Previous UI",
        )

        messages = agent._build_messages_with_context("system")
        rendered = "\n".join(
            block.text
            for message in messages
            for block in message.blocks
            if hasattr(block, "text")
        )

        self.assertIn("<device_state>\nCurrent UI", rendered)
        self.assertNotIn("<previous_device_state>", rendered)
        self.assertNotIn("Previous UI", rendered)

        repeated = agent._build_messages_with_context("system")
        repeated_rendered = "\n".join(
            block.text
            for message in repeated
            for block in message.blocks
            if hasattr(block, "text")
        )
        self.assertIn("<device_state_unchanged/>", repeated_rendered)


# ======================================================================
# 改动 1: 工具定义按需发送 — system_prompt 选择逻辑仿真
# ======================================================================

_FULL_SYSTEM_PROMPT_INTERVAL = 15


class ToolDefinitionsSelectionLogicTest(unittest.TestCase):
    """Simulate the 3-way system_prompt selection logic from
    FastAgent.handle_llm_input, without instantiating the full workflow."""

    @staticmethod
    def _select_prompt(counter: int, optimize: bool) -> str:
        """Replicate the selection logic:
        Returns 'full', 'full_reinject', or 'lite'.
        """
        if counter == 0 or not optimize:
            return "full"
        elif counter % _FULL_SYSTEM_PROMPT_INTERVAL == 0:
            return "full_reinject"
        else:
            return "lite"

    # --- First turn ---

    def test_first_turn_uses_full_prompt_when_optimize_enabled(self):
        """counter=0, optimize=True → full system prompt."""
        self.assertEqual(self._select_prompt(0, True), "full")

    def test_first_turn_uses_full_prompt_when_optimize_disabled(self):
        """counter=0, optimize=False → full system prompt (compat)."""
        self.assertEqual(self._select_prompt(0, False), "full")

    # --- Subsequent turns with optimize ON ---

    def test_turns_1_to_14_use_lite_prompt(self):
        """counter ∈ [1, 14], optimize=True → lite system prompt."""
        for counter in range(1, 15):
            with self.subTest(counter=counter):
                self.assertEqual(self._select_prompt(counter, True), "lite")

    def test_turn_15_reinjects_full_prompt(self):
        """counter=15, optimize=True → full system prompt (re-injection)."""
        self.assertEqual(self._select_prompt(15, True), "full_reinject")

    def test_turn_30_reinjects_full_prompt(self):
        """counter=30, optimize=True → full system prompt (second cycle)."""
        self.assertEqual(self._select_prompt(30, True), "full_reinject")

    def test_turns_16_to_29_use_lite_prompt(self):
        """counter ∈ [16, 29], optimize=True → lite system prompt."""
        for counter in (16, 20, 25, 29):
            with self.subTest(counter=counter):
                self.assertEqual(self._select_prompt(counter, True), "lite")

    # --- Subsequent turns with optimize OFF (compat) ---

    def test_all_turns_use_full_prompt_when_optimize_disabled(self):
        """optimize=False → always full prompt, regardless of counter."""
        for counter in (1, 5, 10, 15, 30, 100):
            with self.subTest(counter=counter):
                self.assertEqual(self._select_prompt(counter, False), "full")

    # --- Edge cases ---

    def test_large_counter_with_optimize_on(self):
        """counter=90 (6×15), optimize=True → full_reinject."""
        self.assertEqual(self._select_prompt(90, True), "full_reinject")

    def test_large_counter_one_after_reinject_with_optimize_on(self):
        """counter=91 (6×15+1), optimize=True → lite."""
        self.assertEqual(self._select_prompt(91, True), "lite")

    def test_fast_agent_uses_same_selection_behavior(self):
        agent = object.__new__(FastAgent)
        agent._optimize_tool_definitions = True

        for counter, expected in ((0, True), (1, False), (14, False), (15, True)):
            with self.subTest(counter=counter):
                agent.tool_call_counter = counter
                self.assertEqual(agent._use_full_system_prompt(), expected)

        agent._optimize_tool_definitions = False
        for counter in (0, 1, 15, 100):
            with self.subTest(counter=counter):
                agent.tool_call_counter = counter
                self.assertTrue(agent._use_full_system_prompt())


if __name__ == "__main__":
    unittest.main()
