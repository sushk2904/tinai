"""
api/providers/__init__.py — Provider Registry (Phase 2.3)

PROVIDER_MAP is the single source of truth for which string names map to
which async callables. The MAB router (Phase 2.5) uses this to call the
selected provider without knowing its internals.

Design: mapping strings → callables keeps the router stateless and testable.
  • Testing: swap a provider for a mock by patching PROVIDER_MAP[name].
  • Adding a provider: add one entry here + one module; router needs no change.

The strings in PROVIDER_MAP MUST match the values in api/redis_keys.PROVIDERS
exactly — that's what the MAB weight keys are keyed on.
"""

from typing import Callable

from api.providers.fallback import call_fallback
from api.providers.groq import call_groq
from api.providers.openrouter import call_openrouter

# Maps provider name strings to their async callable.
# Type: dict[str, Callable[..., Awaitable[ProviderResponse]]]
PROVIDER_MAP: dict[str, Callable] = {
    "groq":        call_groq,
    "openrouter":  call_openrouter,
    "fallback":    call_fallback,
}

__all__ = ["PROVIDER_MAP", "call_groq", "call_openrouter", "call_fallback"]
