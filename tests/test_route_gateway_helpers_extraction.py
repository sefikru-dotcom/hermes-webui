"""Import-resolution and identity tests for the route_gateway_helpers extraction."""

import pathlib


def test_route_gateway_helpers_imports():
    from api.route_gateway_helpers import (
        _gateway_session_metadata_path,
        _load_gateway_session_identity_map,
        _run_gateway_lifecycle_command,
        _safe_first,
    )

    assert callable(_gateway_session_metadata_path)
    assert callable(_load_gateway_session_identity_map)
    assert callable(_run_gateway_lifecycle_command)
    assert _safe_first(None, "  ", "tg") == "tg"


def test_backward_compat_imports():
    from api.routes import (
        _GATEWAY_ACTION_LOCK,
        _gateway_session_metadata_path,
        _load_gateway_session_identity_map,
        _run_gateway_lifecycle_command,
        _safe_first,
    )

    assert callable(_gateway_session_metadata_path)
    assert callable(_load_gateway_session_identity_map)
    assert callable(_run_gateway_lifecycle_command)
    assert hasattr(_GATEWAY_ACTION_LOCK, "acquire")
    assert _safe_first("a", "b") == "a"


def test_identity():
    import api.route_gateway_helpers as helpers
    import api.routes as routes

    assert helpers._MESSAGING_RAW_SOURCES is routes._MESSAGING_RAW_SOURCES
    assert (
        helpers._MESSAGING_SESSION_METADATA_CACHE
        is routes._MESSAGING_SESSION_METADATA_CACHE
    )
    assert (
        helpers._MESSAGING_SESSION_METADATA_LOCK
        is routes._MESSAGING_SESSION_METADATA_LOCK
    )
    assert helpers._GATEWAY_ACTION_LOCK is routes._GATEWAY_ACTION_LOCK
    assert (
        helpers._GATEWAY_LIFECYCLE_TIMEOUT_SECONDS
        is routes._GATEWAY_LIFECYCLE_TIMEOUT_SECONDS
    )
    assert helpers._normalize_messaging_source is routes._normalize_messaging_source
    assert helpers._is_known_messaging_source is routes._is_known_messaging_source
    assert helpers._safe_first is routes._safe_first
    assert (
        helpers._gateway_session_metadata_path is routes._gateway_session_metadata_path
    )
    assert (
        helpers._load_gateway_session_identity_map
        is routes._load_gateway_session_identity_map
    )
    assert (
        helpers._run_gateway_lifecycle_command is routes._run_gateway_lifecycle_command
    )


def test_shared_cache_state_mutation():
    import api.route_gateway_helpers as helpers
    import api.routes as routes

    cache = routes._MESSAGING_SESSION_METADATA_CACHE
    original = dict(cache)
    try:
        cache["path"] = "/tmp/route-gateway-helpers-identity.json"
        assert helpers._MESSAGING_SESSION_METADATA_CACHE["path"] == cache["path"]
    finally:
        cache.clear()
        cache.update(original)


def test_no_circular_import():
    src = (
        pathlib.Path(__file__).parent.parent / "api" / "route_gateway_helpers.py"
    ).read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("from api.routes import") or stripped.startswith(
            "import api.routes"
        ):
            raise AssertionError(
                "route_gateway_helpers must not import api.routes at module scope"
            )
