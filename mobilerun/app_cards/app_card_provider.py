"""
Abstract base class for app card providers.

Providers load app-specific instruction cards based on package names,
bundle identifiers, or domain names.
Supports multiple backends: local files, remote servers, or composite strategies.
"""

from abc import ABC, abstractmethod


class AppCardProvider(ABC):
    """Abstract interface for loading app-specific instruction cards."""

    @abstractmethod
    async def load_app_card(
        self,
        identifier: str,
        instruction: str = "",
        platform: str = "android",
    ) -> str:
        """
        Load app card for a given identifier.

        Args:
            identifier: package_name (Android), bundle_id (iOS), or domain (Web)
            instruction: User's instruction (optional context)
            platform: "android", "ios", or "web"

        Returns:
            App card content string, or empty string if not found
        """
        pass
