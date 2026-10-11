"""Workspace Git routes and safeguards with call-time route dependencies."""

from __future__ import annotations

def _git_session(handler, session_id: str, *, bad, get_session):
    if not session_id:
        bad(handler, "session_id required")
        return None
    try:
        return get_session(session_id)
    except KeyError:
        bad(handler, "Session not found", 404)
        return None


def _git_session_workspace(handler, session_id: str, *, Path, _git_session):
    session = _git_session(handler, session_id)
    if session is None:
        return None
    return Path(session.workspace)


def _git_session_and_workspace(handler, session_id: str, *, Path, _git_session):
    session = _git_session(handler, session_id)
    if session is None:
        return None, None
    return session, Path(session.workspace)


def _git_locked_by_active_stream(session) -> bool:
    stream_id = getattr(session, "active_stream_id", None)
    if not stream_id:
        return False
    try:
        from api.config import STREAMS, STREAMS_LOCK

        with STREAMS_LOCK:
            return stream_id in STREAMS
    except Exception:
        return False


def _git_reject_destructive_if_unsafe(handler, session, *, _git_bad, _git_locked_by_active_stream) -> bool:
    from api.workspace_git import (
        GitWorkspaceError,
        WORKSPACE_GIT_DESTRUCTIVE_ENV,
        workspace_git_destructive_enabled,
    )

    if not workspace_git_destructive_enabled():
        _git_bad(
            handler,
            GitWorkspaceError(
                f"Destructive workspace Git operations are disabled. Set {WORKSPACE_GIT_DESTRUCTIVE_ENV}=1 to enable them.",
                "destructive_git_disabled",
            ),
            status=403,
        )
        return True
    if _git_locked_by_active_stream(session):
        _git_bad(
            handler,
            GitWorkspaceError(
                "A session run is active. Wait for it to finish before running this Git operation.",
                "active_stream",
            ),
            status=409,
        )
        return True
    return False


def _handle_git_status(handler, parsed, *, _git_bad, _git_session_workspace, j, parse_qs):
    qs = parse_qs(parsed.query)
    workspace = _git_session_workspace(handler, qs.get("session_id", [""])[0])
    if workspace is None:
        return True
    try:
        from api.workspace_git import GitWorkspaceError, git_status

        return j(handler, {"git": git_status(workspace)})
    except GitWorkspaceError as e:
        return _git_bad(handler, e)


def _handle_git_branches(handler, parsed, *, _git_bad, _git_session_workspace, j, parse_qs):
    qs = parse_qs(parsed.query)
    workspace = _git_session_workspace(handler, qs.get("session_id", [""])[0])
    if workspace is None:
        return True
    try:
        from api.workspace_git import GitWorkspaceError, git_branches

        return j(handler, {"branches": git_branches(workspace)})
    except GitWorkspaceError as e:
        return _git_bad(handler, e)


def _handle_git_diff(handler, parsed, *, _git_bad, _git_session_workspace, bad, j, parse_qs):
    qs = parse_qs(parsed.query)
    workspace = _git_session_workspace(handler, qs.get("session_id", [""])[0])
    if workspace is None:
        return True
    path = qs.get("path", [""])[0]
    kind = qs.get("kind", ["unstaged"])[0]
    if not path:
        return bad(handler, "path required")
    try:
        from api.workspace_git import GitWorkspaceError, git_diff

        return j(handler, {"diff": git_diff(workspace, path, kind)})
    except GitWorkspaceError as e:
        return _git_bad(handler, e)


def _git_bad(handler, err, status: int=400, *, _sanitize_error, j):
    return j(
        handler,
        {
            "error": _sanitize_error(err),
            "code": getattr(err, "code", "git_failed") or "git_failed",
        },
        status=status,
    )


def _git_paths_from_body(body) -> list[str]:
    raw_paths = body.get("paths")
    if raw_paths is None and body.get("path"):
        raw_paths = [body.get("path")]
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    if not isinstance(raw_paths, list):
        raise ValueError("paths must be a list")
    return [str(path) for path in raw_paths]


def _handle_git_stage(
    handler,
    body,
    *,
    _git_bad,
    _git_paths_from_body,
    _git_reject_destructive_if_unsafe,
    _git_session_and_workspace,
    bad,
    j,
    require,
):
    try:
        require(body, "session_id")
        paths = _git_paths_from_body(body)
        session, workspace = _git_session_and_workspace(handler, body["session_id"])
        if workspace is None:
            return True
        if _git_reject_destructive_if_unsafe(handler, session):
            return True
        from api.workspace_git import GitWorkspaceError, git_stage

        return j(handler, {"ok": True, "git": git_stage(workspace, paths)})
    except ValueError as e:
        return bad(handler, str(e))
    except GitWorkspaceError as e:
        return _git_bad(handler, e)


def _handle_git_unstage(
    handler,
    body,
    *,
    _git_bad,
    _git_paths_from_body,
    _git_reject_destructive_if_unsafe,
    _git_session_and_workspace,
    bad,
    j,
    require,
):
    try:
        require(body, "session_id")
        paths = _git_paths_from_body(body)
        session, workspace = _git_session_and_workspace(handler, body["session_id"])
        if workspace is None:
            return True
        if _git_reject_destructive_if_unsafe(handler, session):
            return True
        from api.workspace_git import GitWorkspaceError, git_unstage

        return j(handler, {"ok": True, "git": git_unstage(workspace, paths)})
    except ValueError as e:
        return bad(handler, str(e))
    except GitWorkspaceError as e:
        return _git_bad(handler, e)


def _handle_git_discard(
    handler,
    body,
    *,
    _git_bad,
    _git_paths_from_body,
    _git_reject_destructive_if_unsafe,
    _git_session_and_workspace,
    bad,
    j,
    require,
):
    try:
        require(body, "session_id")
        paths = _git_paths_from_body(body)
        session, workspace = _git_session_and_workspace(handler, body["session_id"])
        if workspace is None:
            return True
        if _git_reject_destructive_if_unsafe(handler, session):
            return True
        from api.workspace_git import GitWorkspaceError, git_discard

        return j(
            handler,
            {
                "ok": True,
                "git": git_discard(
                    workspace,
                    paths,
                    delete_untracked=bool(body.get("delete_untracked")),
                ),
            },
        )
    except ValueError as e:
        return bad(handler, str(e))
    except GitWorkspaceError as e:
        return _git_bad(handler, e)


def _llm_git_commit_message(
    system_prompt: str,
    user_prompt: str,
    session=None,
    *,
    ensure_agent_runtime_current,
    logger,
    require_ai_agent_class,
    uuid,
) -> str:
    from api import profiles as profiles_api

    active_profile = profiles_api.get_active_profile_name() or "default"
    with profiles_api.profile_env_for_background_worker(
        active_profile,
        "git commit message",
        logger_override=logger,
    ):
        from api.config import (
            get_effective_default_model,
            model_with_provider_context,
            resolve_custom_provider_connection,
            resolve_model_provider,
        )

        session_model = str(getattr(session, "model", "") or "").strip()
        session_provider = str(getattr(session, "model_provider", "") or "").strip() or None
        model_for_resolution = (
            model_with_provider_context(session_model, session_provider)
            if session_model
            else get_effective_default_model()
        )
        _main_model, _main_provider, _main_base_url = resolve_model_provider(model_for_resolution)
        _main_api_key = None
        try:
            from api.oauth import resolve_runtime_provider_with_anthropic_env_lock
            from hermes_cli.runtime_provider import resolve_runtime_provider

            _rt = resolve_runtime_provider_with_anthropic_env_lock(
                resolve_runtime_provider,
                requested=_main_provider,
            )
            _main_api_key = _rt.get("api_key")
            if not _main_provider:
                _main_provider = _rt.get("provider")
            if not _main_base_url:
                _main_base_url = _rt.get("base_url")
        except Exception as _e:
            logger.debug("git commit message runtime provider resolution failed: %s", _e)
        if isinstance(_main_provider, str) and _main_provider.startswith("custom:"):
            _cp_key, _cp_base = resolve_custom_provider_connection(_main_provider)
            if not _main_api_key and _cp_key:
                _main_api_key = _cp_key
            if not _main_base_url and _cp_base:
                _main_base_url = _cp_base

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        main_runtime = {
            "provider": _main_provider,
            "model": _main_model,
            "base_url": _main_base_url,
            "api_key": _main_api_key,
        }
        ensure_agent_runtime_current()
        try:
            from agent.auxiliary_client import get_text_auxiliary_client

            aux_client, aux_model = get_text_auxiliary_client(
                "compression",
                main_runtime=main_runtime,
            )
            if aux_client is not None and aux_model:
                response = aux_client.chat.completions.create(
                    model=aux_model,
                    messages=messages,
                )
                return str(response.choices[0].message.content or "").strip()
        except Exception as _e:
            logger.debug("git commit message auxiliary model failed; falling back to main model: %s", _e)

        AIAgent = require_ai_agent_class()

        agent = AIAgent(
            model=_main_model,
            provider=_main_provider,
            base_url=_main_base_url,
            api_key=_main_api_key,
            platform="webui",
            quiet_mode=True,
            enabled_toolsets=[],
            session_id=f"git-commit-message-{uuid.uuid4().hex[:8]}",
        )
        result = agent.run_conversation(
            user_message=user_prompt,
            system_message=system_prompt,
            conversation_history=[],
            task_id=f"git-commit-message-{uuid.uuid4().hex[:8]}",
        )
        return str(result.get("final_response") or "").strip()


def _handle_git_commit_message(
    handler,
    body,
    *,
    AgentRuntimeChangedError,
    Path,
    _git_bad,
    _llm_git_commit_message,
    _sanitize_error,
    agent_runtime_stale_payload,
    bad,
    get_session,
    j,
    logger,
    require,
):
    from api.workspace_git import (
        GitWorkspaceError,
        clean_generated_commit_message,
        staged_commit_message_prompt,
    )

    try:
        require(body, "session_id")
        session = get_session(body["session_id"])
        workspace = Path(session.workspace)

        prompt = staged_commit_message_prompt(workspace)
        message = clean_generated_commit_message(
            _llm_git_commit_message(prompt["system_prompt"], prompt["user_prompt"], session=session)
        )
        if not message:
            raise GitWorkspaceError("No commit message was generated")
        return j(handler, {"ok": True, "message": message, "truncated": bool(prompt.get("truncated"))})
    except KeyError:
        return bad(handler, "Session not found", 404)
    except ValueError as e:
        return bad(handler, str(e))
    except GitWorkspaceError as e:
        return _git_bad(handler, e)
    except AgentRuntimeChangedError as e:
        return j(handler, agent_runtime_stale_payload(e), status=409)
    except Exception as e:
        logger.exception("git commit message generation failed")
        return bad(handler, _sanitize_error(e), 500)


def _handle_git_commit_message_selected(
    handler,
    body,
    *,
    AgentRuntimeChangedError,
    Path,
    _git_bad,
    _git_paths_from_body,
    _llm_git_commit_message,
    _sanitize_error,
    agent_runtime_stale_payload,
    bad,
    get_session,
    j,
    logger,
    require,
):
    from api.workspace_git import (
        GitWorkspaceError,
        clean_generated_commit_message,
        selected_commit_message_prompt,
    )

    try:
        require(body, "session_id")
        paths = _git_paths_from_body(body)
        session = get_session(body["session_id"])
        workspace = Path(session.workspace)

        prompt = selected_commit_message_prompt(workspace, paths)
        message = clean_generated_commit_message(
            _llm_git_commit_message(prompt["system_prompt"], prompt["user_prompt"], session=session)
        )
        if not message:
            raise GitWorkspaceError("No commit message was generated")
        return j(handler, {"ok": True, "message": message, "truncated": bool(prompt.get("truncated"))})
    except KeyError:
        return bad(handler, "Session not found", 404)
    except ValueError as e:
        return bad(handler, str(e))
    except GitWorkspaceError as e:
        return _git_bad(handler, e)
    except AgentRuntimeChangedError as e:
        return j(handler, agent_runtime_stale_payload(e), status=409)
    except Exception as e:
        logger.exception("selected git commit message generation failed")
        return bad(handler, _sanitize_error(e), 500)


def _handle_git_commit(
    handler,
    body,
    *,
    _git_bad,
    _git_reject_destructive_if_unsafe,
    _git_session_and_workspace,
    bad,
    j,
    require,
):
    try:
        require(body, "session_id", "message")
        session, workspace = _git_session_and_workspace(handler, body["session_id"])
        if workspace is None:
            return True
        if _git_reject_destructive_if_unsafe(handler, session):
            return True
        from api.workspace_git import GitWorkspaceError, git_commit

        return j(handler, git_commit(workspace, body.get("message", "")))
    except ValueError as e:
        return bad(handler, str(e))
    except GitWorkspaceError as e:
        return _git_bad(handler, e)


def _handle_git_commit_selected(
    handler,
    body,
    *,
    _git_bad,
    _git_paths_from_body,
    _git_reject_destructive_if_unsafe,
    _git_session_and_workspace,
    bad,
    j,
    require,
):
    try:
        require(body, "session_id", "message")
        paths = _git_paths_from_body(body)
        session, workspace = _git_session_and_workspace(handler, body["session_id"])
        if workspace is None:
            return True
        if _git_reject_destructive_if_unsafe(handler, session):
            return True
        from api.workspace_git import GitWorkspaceError, git_commit_selected

        return j(handler, git_commit_selected(workspace, body.get("message", ""), paths))
    except ValueError as e:
        return bad(handler, str(e))
    except GitWorkspaceError as e:
        return _git_bad(handler, e)


def _handle_git_remote_action(
    handler,
    body,
    action: str,
    *,
    _git_bad,
    _git_reject_destructive_if_unsafe,
    _git_session_and_workspace,
    bad,
    j,
    require,
):
    try:
        require(body, "session_id")
        session, workspace = _git_session_and_workspace(handler, body["session_id"])
        if workspace is None:
            return True
        if action in {"pull", "push"} and _git_reject_destructive_if_unsafe(handler, session):
            return True
        from api.workspace_git import GitWorkspaceError, git_fetch, git_pull, git_push

        actions = {
            "fetch": git_fetch,
            "pull": git_pull,
            "push": git_push,
        }
        return j(handler, actions[action](workspace))
    except ValueError as e:
        return bad(handler, str(e))
    except GitWorkspaceError as e:
        return _git_bad(handler, e)


def _handle_git_checkout(
    handler,
    body,
    *,
    _git_bad,
    _git_reject_destructive_if_unsafe,
    _git_session_and_workspace,
    bad,
    j,
    require,
):
    try:
        require(body, "session_id", "ref", "mode")
        session, workspace = _git_session_and_workspace(handler, body["session_id"])
        if workspace is None:
            return True
        if _git_reject_destructive_if_unsafe(handler, session):
            return True
        from api.workspace_git import GitWorkspaceError, git_checkout

        result = git_checkout(
            workspace,
            str(body.get("ref", "")),
            str(body.get("mode", "local")),
            new_branch=body.get("new_branch"),
            track=bool(body.get("track")),
            dirty_mode=str(body.get("dirty_mode", "block")),
        )
        return j(
            handler,
            {
                "ok": True,
                "git": result.get("status"),
                "branches": result.get("branches"),
                "current_branch": result.get("current_branch"),
                "message": result.get("message", ""),
            },
        )
    except ValueError as e:
        return bad(handler, str(e))
    except GitWorkspaceError as e:
        return _git_bad(handler, e)


def _handle_git_stash_checkout(
    handler,
    body,
    *,
    _git_bad,
    _git_reject_destructive_if_unsafe,
    _git_session_and_workspace,
    bad,
    j,
    require,
):
    try:
        require(body, "session_id", "ref", "mode")
        session, workspace = _git_session_and_workspace(handler, body["session_id"])
        if workspace is None:
            return True
        if _git_reject_destructive_if_unsafe(handler, session):
            return True
        from api.workspace_git import GitWorkspaceError, git_stash_and_checkout

        result = git_stash_and_checkout(
            workspace,
            str(body.get("ref", "")),
            str(body.get("mode", "local")),
            new_branch=body.get("new_branch"),
            track=bool(body.get("track")),
        )
        return j(
            handler,
            {
                "ok": True,
                "git": result.get("status"),
                "branches": result.get("branches"),
                "current_branch": result.get("current_branch"),
                "message": result.get("message", ""),
                "stash_name": result.get("stash_name", ""),
                "stashed": bool(result.get("stashed")),
                "restored_stash": result.get("restored_stash"),
                "restore_failed": bool(result.get("restore_failed")),
                "restore_error": result.get("restore_error", ""),
                "restore_stash": result.get("restore_stash"),
            },
        )
    except ValueError as e:
        return bad(handler, str(e))
    except GitWorkspaceError as e:
        return _git_bad(handler, e)
