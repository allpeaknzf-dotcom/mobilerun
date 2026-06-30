"""
Composite app card provider.

Tries server first, falls back to local if server fails or returns empty.
"""

import logging
from typing import Dict

from mobilerun.app_cards.app_card_provider import AppCardProvider
from mobilerun.app_cards.providers.local_provider import LocalAppCardProvider
from mobilerun.app_cards.providers.server_provider import ServerAppCardProvider

logger = logging.getLogger("mobilerun")


class CompositeAppCardProvider(AppCardProvider):
    """
    Load app cards from server with local fallback.

    Strategy:
    1. Try server first
    2. If server fails or returns empty, try local
    3. Return first non-empty result, or empty if both fail
    """

    def __init__(
        self,
        server_url: str,
        app_cards_dir: str = "config/app_cards",
        server_timeout: float = 2.0,
        server_max_retries: int = 2,
    ):
        """
        Initialize composite provider.

        Args:
            server_url: Base URL of the app card server
            app_cards_dir: Directory containing local app_cards.json
            server_timeout: Server request timeout in seconds
            server_max_retries: Number of server retry attempts
        """
        self.server_provider = ServerAppCardProvider(
            server_url=server_url,
            timeout=server_timeout,
            max_retries=server_max_retries,
        )
        self.local_provider = LocalAppCardProvider(app_cards_dir=app_cards_dir)

    async def load_app_card(self, identifier: str, instruction: str = "", platform: str = "android") -> str:
        """
        Load app card with server-first, local-fallback strategy.

        Args:
            identifier: package_name (Android), bundle_id (iOS), or domain (Web)
            instruction: User instruction/goal

        Returns:
            App card content from server or local, or empty string if both fail
        """
        if not identifier:
            return ""

        # Try server first
        server_result = await self.server_provider.load_app_card(
            identifier, instruction
        )

        if server_result:
            return server_result

        # Server failed or returned empty, try local
        logger.debug(f"Composite provider: falling back to local for {identifier}")
        local_result = await self.local_provider.load_app_card(
            identifier, instruction
        )

        if local_result:
            logger.debug(f"Composite provider: using local fallback for {identifier}")
        else:
            logger.debug(f"Composite provider: no app card found for {identifier}")

        return local_result

    def clear_cache(self) -> None:
        """Clear caches in both providers."""
        self.server_provider.clear_cache()
        self.local_provider.clear_cache()
        logger.debug("Composite app card cache cleared")

    def get_cache_stats(self) -> Dict[str, any]:
        """
        Get cache statistics from both providers.

        Returns:
            Dict with cache stats from server and local providers
        """
        return {
            "server": self.server_provider.get_cache_stats(),
            "local": self.local_provider.get_cache_stats(),
        }
