"""Clients LLM complémentaires (fallback HTTP)."""

from .http_fallback import OpenAIHTTPFallbackClient
from .voice_fallback import HTTPVoiceFallback, VoiceFallbackConfig

__all__ = ["OpenAIHTTPFallbackClient", "HTTPVoiceFallback", "VoiceFallbackConfig"]
