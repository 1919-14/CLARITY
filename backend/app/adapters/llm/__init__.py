"""LLM provider adapters for the optional LLM Scientist (Issue #41).

Provider-specific SDKs stay behind the ``LLMProposalProvider`` interface. The
factory is the only place that maps configuration to a concrete adapter, so the
discovery engine never imports provider code.

Deterministic-only remains the default: :func:`build_llm_scientist_from_environment`
returns ``None`` unless ``LLM_SCIENTIST_ENABLED`` is explicitly true.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Optional

from ...domain.hypothesis_models import ModelFamilyRegistry
from ...domain.llm_scientist import LLMScientist
from ...domain.llm_scientist_contracts import (
    DEFAULT_PROVIDER_ID,
    LLMScientistConfig,
)
from .groq_provider import GroqProposalProvider

SUPPORTED_PROVIDERS = (DEFAULT_PROVIDER_ID,)


def build_provider(
    config: LLMScientistConfig,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> object:
    """Build the configured provider adapter.

    Raises ``ValueError`` for an unsupported provider identifier so a
    misconfiguration is explicit rather than silently falling back.
    """
    if not isinstance(config, LLMScientistConfig):
        raise ValueError("config must be an LLMScientistConfig.")
    if config.provider == DEFAULT_PROVIDER_ID:
        return GroqProposalProvider(config, environ=environ)
    raise ValueError(
        f"Unsupported LLM Scientist provider {config.provider!r}; "
        f"supported providers: {list(SUPPORTED_PROVIDERS)}."
    )


def build_llm_scientist_from_environment(
    *,
    environ: Optional[Mapping[str, str]] = None,
    registry: Optional[ModelFamilyRegistry] = None,
    config: Optional[LLMScientistConfig] = None,
) -> Optional[LLMScientist]:
    """Return a configured scientist, or ``None`` when the feature is disabled.

    A missing credential does not raise here: the first proposal round reports a
    structured ``MISSING_CREDENTIALS`` outcome so the run can fall back visibly
    instead of crashing with a secret-free but opaque startup error.
    """
    resolved = config if config is not None else LLMScientistConfig.from_environment(environ)
    if not isinstance(resolved, LLMScientistConfig):
        raise ValueError("config must be an LLMScientistConfig.")
    if not resolved.enabled:
        return None
    provider = build_provider(resolved, environ=environ)
    return LLMScientist(resolved, provider, registry=registry)


__all__ = [
    "SUPPORTED_PROVIDERS",
    "GroqProposalProvider",
    "build_provider",
    "build_llm_scientist_from_environment",
]
