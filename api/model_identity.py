"""Provider-aware model identity and reasoning-pin resolution.

This module is the single source of truth for two questions the WebUI kept
answering in several slightly-different ways:

1. **"Is this the same model?"** A model id is only unique *together with its
   provider*: ``openrouter``'s ``anthropic/claude-sonnet-4`` and a local
   ``custom:ollama`` serving the same id are different lanes, and two providers
   can legitimately expose the same bare id. Model ids are therefore never
   rewritten here: the vendor slash (``vendor/model``) and the route suffix
   (``:free``, ``:kiro``, ``:beta``, ...) are part of the identity. This module
   does not decide which suffixes upstream accepts — only the model catalog can
   — it just refuses to destroy them.

2. **"Which reasoning level is in force?"** Runtime priority is
   ``session override > per-model pin (agent.reasoning_overrides) > global``.
   Pins written by this WebUI are provider-qualified (``@provider:model``) so the
   same model id on two providers keeps two independent levels. Pins written by
   older builds used the bare model id; those keys stay readable so an existing
   profile keeps working without a config migration.

Layering: pure domain logic, no I/O, no config reads. Callers in ``api.config``
(persistence), ``api.routes`` (HTTP boundaries) and ``api.streaming`` (runtime)
pass in the already-loaded values, which keeps the rules identical on every
path. ``config`` supplies its provider tables to :func:`parse_route_hint` so the
``@provider:model`` grammar has exactly one implementation.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

__all__ = [
    "ModelIdentity",
    "ModelIdentityError",
    "REASONING_SOURCES",
    "identities_match",
    "identity",
    "match_reasoning_pin",
    "match_spelling_key",
    "normalize_model_id",
    "normalize_provider_id",
    "normalize_reasoning_effort",
    "parse_route_hint",
    "reasoning_pin_candidates",
    "reasoning_pin_key",
    "resolve_reasoning_effort",
    "split_model_identity",
    "validate_model_id",
    "validate_optional_model_id",
    "validate_provider_id",
]

#: Canonical spelling of the reasoning sources, in priority order.
REASONING_SOURCES = ("session", "model", "default")

# Provider ids that mean "no explicit provider"; they must never take part in an
# identity comparison (otherwise "default" and "" look like different lanes).
_NON_PROVIDERS = frozenset({"", "default", "auto", "none"})

# Characters that can never appear in a model id. Rejecting them at the API
# boundary stops a pasted newline / NUL from becoming a persisted model, while
# everything else (slashes, colons, dots, unicode) is preserved verbatim.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

_SPELLING_SEPARATORS = re.compile(r"[-_.\s]+")


class ModelIdentityError(ValueError):
    """Invalid model/provider identity supplied by a caller.

    Subclasses ``ValueError`` so every existing ``except ValueError`` handler in
    the routes layer keeps mapping it to a 400 without changes. Carries the
    offending fields so an error response can tell the client *which* model and
    provider were rejected instead of a bare message.
    """

    code = "invalid_model_identity"

    def __init__(
        self,
        message: str,
        *,
        field: str = "model",
        model: str | None = None,
        provider: str | None = None,
        code: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.field = field
        self.model = model
        self.provider = provider
        if code:
            self.code = code

    def as_dict(self) -> dict:
        """Diagnostic payload for an HTTP error body."""
        payload: dict = {"message": self.message, "field": self.field, "code": self.code}
        if self.model is not None:
            payload["model"] = self.model
        if self.provider is not None:
            payload["provider"] = self.provider
        return payload


@dataclass(frozen=True)
class ModelIdentity:
    """A model id plus the provider lane it belongs to.

    ``provider`` is ``None`` when the caller genuinely does not know it (legacy
    sessions stored only a model id). ``key`` is the canonical provider-qualified
    spelling used for per-model reasoning pins and log lines.
    """

    model: str
    provider: str | None = None

    @property
    def key(self) -> str:
        if self.provider:
            return f"@{self.provider}:{self.model}"
        return self.model

    @property
    def is_valid(self) -> bool:
        return bool(self.model)

    def as_dict(self) -> dict:
        return {"model": self.model, "provider": self.provider, "key": self.key}


def normalize_model_id(value: object) -> str:
    """Strip surrounding whitespace only.

    Deliberately does **not** touch the id itself: vendor slashes and route
    suffixes are part of the identity.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return value.strip()


def normalize_provider_id(value: object) -> str | None:
    """Canonical provider id, or ``None`` for the "no provider" sentinels."""
    raw = str(value or "").strip().lower()
    if raw in _NON_PROVIDERS:
        return None
    return raw


def validate_model_id(
    value: object,
    *,
    field: str = "model",
    require_model_after_hint: bool = True,
) -> str:
    """Return the model id unchanged (whitespace-trimmed) or raise.

    Used at API boundaries (session create/update, chat start, reasoning pin
    writes). An empty or blank id used to be persisted silently and then made the
    session unroutable; a non-string (``{}``/``[]``/``123``) used to be
    stringified into a nonsense id. Both are rejected here.
    """
    if value is None:
        raise ModelIdentityError(f"{field} is required", field=field, code="model_required")
    if not isinstance(value, str):
        raise ModelIdentityError(
            f"{field} must be a string (got {type(value).__name__})",
            field=field,
            model=repr(value),
            code="model_not_a_string",
        )
    model = value.strip()
    if not model:
        raise ModelIdentityError(f"{field} must not be empty", field=field, code="model_empty")
    if _CONTROL_CHARS.search(model):
        raise ModelIdentityError(
            f"{field} contains control characters",
            field=field,
            model=model.encode("unicode_escape").decode("ascii", "replace"),
            code="model_control_chars",
        )
    # A route hint with an empty tail (``@custom:plusvibe-api:``) carries no
    # model at all — that shape reached sessions in production and made them
    # unroutable. This is identity-level emptiness, NOT an upstream verdict: the
    # id itself (``vendor/model``, ``:free``, ``:kiro``) is never second-guessed
    # here, because only the provider catalog can say what it serves.
    if require_model_after_hint and model.startswith("@") and ":" in model:
        parsed = parse_route_hint(model)
        if parsed and not normalize_model_id(parsed[0]):
            raise ModelIdentityError(
                f"{field} has an empty model after the provider hint",
                field=field,
                model=model,
                provider=parsed[1],
                code="model_empty_tail",
            )
    return model


def validate_optional_model_id(
    value: object,
    *,
    field: str = "model",
    require_model_after_hint: bool = False,
) -> str:
    """Like :func:`validate_model_id` but treats ``None``/blank as "unset".

    Auxiliary slots use an empty model to mean "auto", so blank is legal there;
    a non-string shape or control characters are still rejected. The
    empty-route-hint-tail rule is off by default because auxiliary slots have
    their own, more specific rejection (``_provider_native_auxiliary_model``)
    that tells the user the qualified form must match the selected provider.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ModelIdentityError(
            f"{field} must be a string (got {type(value).__name__})",
            field=field,
            model=repr(value),
            code="model_not_a_string",
        )
    model = value.strip()
    if not model:
        return ""
    return validate_model_id(
        model,
        field=field,
        require_model_after_hint=require_model_after_hint,
    )


def validate_provider_id(value: object, *, field: str = "model_provider") -> str | None:
    """Return the provider id, or ``None``; raise only for a non-string shape."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModelIdentityError(
            f"{field} must be a string (got {type(value).__name__})",
            field=field,
            provider=repr(value),
            code="provider_not_a_string",
        )
    return normalize_provider_id(value)


def _default_custom_endpoint_slug(rest: str) -> bool:
    """True when ``custom:<rest>`` is an endpoint-style ``host:port`` slug."""
    rest = str(rest or "").strip()
    if ":" not in rest:
        return False
    host, port_s = rest.rsplit(":", 1)
    if not host or ":" in host or not port_s.isdigit():
        return False
    try:
        port_n = int(port_s)
    except ValueError:
        return False
    if not 1 <= port_n <= 65535:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def parse_route_hint(
    model_id: object,
    *,
    known_providers=(),
    custom_endpoint_slug=None,
) -> tuple[str, str] | None:
    """Parse a WebUI ``@provider:model`` route hint into ``(model, provider)``.

    The provider segment may contain colons for named custom providers, while the
    model segment may contain colons for tags such as ``:free``. ``known_providers``
    lets the caller supply the provider tables so ``@openai:gpt-5:beta`` splits on
    the *first* colon for a known provider and on the last one otherwise.
    """
    candidate = str(model_id or "").strip()
    if not candidate.startswith("@") or ":" not in candidate:
        return None
    endpoint_check = custom_endpoint_slug or _default_custom_endpoint_slug
    inner = candidate[1:]
    provider_hint, bare_model = inner.rsplit(":", 1)
    if provider_hint.startswith("custom:") and provider_hint.count(":") >= 2:
        slug_rest = provider_hint[len("custom:"):]
        if not endpoint_check(slug_rest):
            provider_hint, extra = provider_hint.rsplit(":", 1)
            bare_model = f"{extra}:{bare_model}"
    elif provider_hint not in known_providers and not provider_hint.startswith("custom:"):
        provider_hint, bare_model = inner.split(":", 1)
    return bare_model, provider_hint


def identity(model_id: object, provider: object = None) -> ModelIdentity:
    """Build a :class:`ModelIdentity` without raising (invalid ids stay empty)."""
    return ModelIdentity(
        normalize_model_id(model_id),
        normalize_provider_id(provider),
    )


def split_model_identity(
    model_id: object,
    provider: object = None,
    *,
    known_providers=(),
    allow_slash_prefix: bool = False,
    custom_endpoint_slug=None,
) -> ModelIdentity:
    """Normalize one stored model value into a comparable identity.

    Handles the two provider-qualified shapes the WebUI has persisted:

    * ``@provider:model`` — the internal route hint;
    * ``provider/model`` — legacy OpenRouter-style storage, only when
      ``allow_slash_prefix`` is set (the context-window guard needs it so a
      reload of a slash-stored model is not mistaken for a model change).

    ``allow_slash_prefix`` is opt-in precisely because ``vendor/model`` and
    ``provider/model`` are indistinguishable in general; only callers that
    already treat the leading segment as a provider may ask for it.
    """
    raw = normalize_model_id(model_id)
    explicit = normalize_provider_id(provider)
    if not raw:
        return ModelIdentity("", explicit)

    parsed = parse_route_hint(
        raw, known_providers=known_providers, custom_endpoint_slug=custom_endpoint_slug
    )
    if parsed:
        bare, hint = parsed
        bare = normalize_model_id(bare)
        if bare:
            return ModelIdentity(bare, normalize_provider_id(hint) or explicit)

    if allow_slash_prefix and "/" in raw:
        prefix, rest = raw.split("/", 1)
        prefix = prefix.strip()
        rest = rest.strip()
        if prefix and rest:
            return ModelIdentity(rest, normalize_provider_id(prefix) or explicit)

    return ModelIdentity(raw, explicit)


def identities_match(
    stored_model: object,
    stored_provider: object,
    resolved_model: object,
    resolved_provider: object,
    *,
    known_providers=(),
    allow_slash_prefix: bool = False,
    provider_normalizer=None,
    custom_endpoint_slug=None,
) -> bool:
    """True when two (model, provider) pairs describe the same lane.

    Provider comparison uses ``provider_normalizer`` (the routes layer passes its
    alias-resolving canonicalizer) so ``openai-codex`` and ``codex`` do not look
    like two lanes. An unknown provider on either side degrades to a bare model
    comparison — that is the legacy shape and must not report a model change.
    """
    left = split_model_identity(
        stored_model,
        stored_provider,
        known_providers=known_providers,
        allow_slash_prefix=allow_slash_prefix,
        custom_endpoint_slug=custom_endpoint_slug,
    )
    right = split_model_identity(
        resolved_model,
        resolved_provider,
        known_providers=known_providers,
        allow_slash_prefix=allow_slash_prefix,
        custom_endpoint_slug=custom_endpoint_slug,
    )
    if not left.model or not right.model:
        return False

    normalize = provider_normalizer or normalize_provider_id
    left_provider = normalize(left.provider) or ""
    right_provider = normalize(right.provider) or ""
    if left.model != right.model:
        return False
    if left_provider and right_provider:
        return left_provider == right_provider
    return True


# ── Reasoning pins ────────────────────────────────────────────────────────────


def normalize_reasoning_effort(value: object) -> str | None:
    """Lower-case a reasoning level; empty / ``None`` becomes ``None``."""
    raw = str(value or "").strip().lower()
    return raw or None


def reasoning_pin_key(model_id: object, provider: object = None) -> str:
    """Canonical ``agent.reasoning_overrides`` key for a model lane.

    Provider-qualified when the provider is known, bare otherwise (the legacy
    shape older builds wrote).
    """
    model = normalize_model_id(model_id)
    if not model:
        return ""
    parsed = parse_route_hint(model)
    if parsed:
        bare, hint = parsed
        bare = normalize_model_id(bare)
        provider = normalize_provider_id(hint) or provider
        model = bare or model
    provider_id = normalize_provider_id(provider)
    return f"@{provider_id}:{model}" if provider_id else model


def _spelling_key(value: object) -> str:
    return _SPELLING_SEPARATORS.sub("-", str(value or "").strip().lower())


def match_spelling_key(existing, target: str) -> str | None:
    """Find a stored key that differs only in ``-``/``_``/``.``/space spelling."""
    if not target:
        return None
    want = _spelling_key(target)
    if not want:
        return None
    for key in existing:
        if _spelling_key(key) == want:
            return key
    return None


def reasoning_pin_candidates(model_id: object, provider: object = None) -> list[str]:
    """Ordered keys to look up for a model lane, most specific first.

    1. provider-qualified key (new writer);
    2. bare model id (legacy writer);
    3. last path segment of the model id (legacy vendor-prefix tolerance).

    A pin stored under a *different* provider's qualified key is never a
    candidate, so the same model id on two providers keeps independent levels.
    """
    model = normalize_model_id(model_id)
    if not model:
        return []
    provider_id = normalize_provider_id(provider)
    parsed = parse_route_hint(model)
    if parsed:
        bare, hint = parsed
        bare = normalize_model_id(bare)
        provider_id = normalize_provider_id(hint) or provider_id
        model = bare or model

    candidates: list[str] = []
    if provider_id:
        candidates.append(f"@{provider_id}:{model}")
    candidates.append(model)
    if "/" in model:
        suffix = model.rsplit("/", 1)[-1].strip()
        if suffix and suffix != model:
            candidates.append(suffix)
    return candidates


def match_reasoning_pin(
    overrides: object,
    model_id: object,
    provider: object = None,
) -> tuple[str | None, str | None]:
    """Return ``(stored_key, effort)`` for a model lane, or ``(None, None)``.

    Each candidate is tried exactly first, then spelling-insensitively, so a pin
    written as ``gpt-5.4`` still matches a session id of ``gpt-5-4`` without
    minting a near-duplicate.
    """
    if not isinstance(overrides, dict) or not overrides:
        return None, None
    for candidate in reasoning_pin_candidates(model_id, provider):
        effort = normalize_reasoning_effort(overrides.get(candidate))
        if effort:
            return candidate, effort
        matched = match_spelling_key(overrides, candidate)
        if matched is not None:
            effort = normalize_reasoning_effort(overrides.get(matched))
            if effort:
                return matched, effort
    return None, None


def resolve_reasoning_effort(
    *,
    session_effort: object = None,
    overrides: object = None,
    global_effort: object = None,
    model_id: object = None,
    provider: object = None,
) -> dict:
    """Resolve the runtime reasoning level: session > pin > global.

    Returns a diagnostic dict rather than a bare string so the caller can report
    *why* a level is in force (``source``) and which stored key won
    (``pin_key``) — the fields the reasoning chip and the error payloads need.
    """
    session_value = normalize_reasoning_effort(session_effort)
    pin_key, pin_value = match_reasoning_pin(overrides, model_id, provider)
    global_value = normalize_reasoning_effort(global_effort)

    if session_value:
        effort, source = session_value, "session"
    elif pin_value:
        effort, source = pin_value, "model"
    else:
        effort, source = global_value, "default"

    return {
        "effort": effort,
        "source": source,
        "pin_key": pin_key,
        "pin_effort": pin_value,
        "global_effort": global_value,
        "session_effort": session_value,
        "candidates": reasoning_pin_candidates(model_id, provider),
        "model": normalize_model_id(model_id),
        "provider": normalize_provider_id(provider),
    }
