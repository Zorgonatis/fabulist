"""Connection registry and OpenAI-compatible client factory (PLAN R5)."""

from .client import (
    DEFAULT_CALL_TIMEOUT_S,
    DEFAULT_SAMPLING,
    KEYLESS_API_KEY,
    ModelCallDeadlineExceeded,
    build_request,
    call_deadline_s,
    configure,
    get_client,
    merge_extra_body,
    merge_sampling,
    set_registry,
)
from .capabilities import (
    CAPABILITY_KEYS,
    DEFAULT_CAPABILITIES,
    PROVIDER_PROFILES,
    provider_family,
    resolve_capabilities,
)
from .registry import Connection, ConnectionConfigError, Registry, load_registry

__all__ = [
    "CAPABILITY_KEYS",
    "DEFAULT_CALL_TIMEOUT_S",
    "Connection",
    "ConnectionConfigError",
    "DEFAULT_CAPABILITIES",
    "DEFAULT_SAMPLING",
    "KEYLESS_API_KEY",
    "ModelCallDeadlineExceeded",
    "PROVIDER_PROFILES",
    "Registry",
    "build_request",
    "configure",
    "get_client",
    "load_registry",
    "merge_extra_body",
    "merge_sampling",
    "provider_family",
    "resolve_capabilities",
    "set_registry",
]
