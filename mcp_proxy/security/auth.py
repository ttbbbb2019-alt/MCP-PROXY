from __future__ import annotations

from typing import Optional


class AuthManager:
    """Simple token-based authenticator that can be swapped with real implementations."""

    def __init__(self, shared_token: Optional[str] = None) -> None:
        self._token = shared_token

    def is_configured(self) -> bool:
        return bool(self._token)

    def validate(self, presented: Optional[str]) -> bool:
        if not self._token:
            return True
        return presented == self._token
