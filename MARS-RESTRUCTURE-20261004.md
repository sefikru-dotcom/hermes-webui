# Mars structural pass — 2026-10-04

## Scope and outcome

Baseline: `8fdcebfc`, Mars custom product, not upstream Hermes.
Implementation work is on `mars-restructure-20261004`; production was not deployed
or restarted. The production checkout stays on `8fdcebfc` and health remains OK.

- 99 original functions moved into six domain implementation modules.
- `api/routes.py`: 28,174 → 26,260 lines, −1,914 (6.8%).
- HTTP paths, JSON/audio/file payloads, facade signatures, and exported helper names
  are preserved. No static/UI files or model/provider defaults were changed.
- Function bodies are AST-equivalent to baseline; only their explicit dependency
  signatures and facade delegation changed.
- 21 added compatibility/behavior tests cover call-time dependencies and ownership.
- No namespace copying, global synchronization, upstream merge, or new dependencies.

## Domain map

| Domain | Module | Functions |
|---|---|---:|
| Wiki discovery/cache/status | `api/route_wiki_helpers.py` | 16 |
| Memory and project context | `api/route_memory.py` | 7 |
| Workspace Git | `api/route_git.py` | 21 |
| Skills | `api/route_skills.py` | 20 |
| File/folder/escape/workspace routes | `api/route_files.py` | 27 |
| Anchored file responses | `api/file_response_helpers.py` | 8 |

Navigation and ownership rules are in `MARS-ARCHITECTURE.md`.

## Follow-up structural slice — anchor activity scenes

The next extraction moved the pure anchor-activity scene domain into
api/anchor_activity_scene.py: message/content normalization, activity-row projection,
tool matching/merging, live-row settlement, and persisted-scene hydration. The route-level
persistence handler, session materialization, visibility checks, and compatibility facades
remain in api/routes.py. This was a mechanical boundary change only; no UI, provider,
streaming lifecycle, session ownership, or inherited baseline failure was changed.

## Verification

Tests use `./scripts/test.sh`, isolated state/home/ports from `tests/conftest.py`.
No real credentials, sessions, skills, music cache, or runtime profiles were changed.

Focused results:

- Wiki/read-only neighbors: 70 passed.
- Memory/context/redaction neighbors: 137 passed, 1 skipped.
- Git/workspace panel plus new seams: 102 passed.
- Skills/Wiki plus new seams: 57 passed, 10 skipped.
- Files and Git/skills seam checks: 329 passed.
- Agent-source dependency audit, workspace validation and media: 81 passed.

Large route-contract comparison (187/186 test files; source-test discovery changes
when checks are redirected from routes.py to actual implementation modules):

| Revision | Passed | Failed | Skipped | Total |
|---|---:|---:|---:|---:|
| Baseline `8fdcebfc` | 3,100 | 74 | 58 | 3,232 |
| Final structural pass | 3,116 | 74 | 58 | 3,248 |

The failed node-id sets are identical: **zero new regressions**. Initial broad
verification found four new source-location failures; all four were corrected by
following actual module anchors, and the full comparison was repeated. Existing
failures were not suppressed or changed to make Mars conform to inherited Hermes
branding, update behavior, English diagnostics, or legacy UI assumptions.

A separate clean gate explicitly deselected only the 74 node IDs proven failing
on the original baseline: **3,116 passed, 58 skipped, 74 deselected**. This is a
scoped refactoring gate, not a claim that the original full suite is green.
The raw final comparison above still includes all 74 failures.

An additional skills/Wiki/handoff run with the installed Agent path explicitly
set (`HERMES_WEBUI_AGENT_DIR=/usr/local/lib/hermes-agent`) gave 83 passed and 10
skipped. Those remaining skips are optional-module availability requirements;
they are not hidden by blanket test exclusions.

Ruff checks all six extracted modules and five new test modules; diff-scoped Ruff
against `8fdcebfc` reports zero new violations. `git diff --check` passes.
The 99 moved bodies and legacy facade signatures/exports were checked separately.
No browser visual test was run: this is a backend-only mechanical extraction with
no changes to `static/`, not a UI change. The entire ~11,000-test suite was not run;
the broad gate covers route-source audits and all direct extracted-function callers.

## Parallel work and cleanup

Two isolated worker worktrees were used. Their intermediate implementation was
not accepted when it failed to provide a proven extraction; the final domains were
implemented and checked by the coordinator. The unaccepted worker commit remains
outside the integration branch. All three temporary worker worktrees, their .venv
links, scratch modules and session backup were removed. Raw verification XML files
are temporary and will be removed after recording results here.

No push to the foreign public upstream, service restart, or production release.

## Confirmed baseline failures

These are recorded rather than fixed because the requested scope is restructuring.
The exact list allows future work to distinguish inherited/test-contract debt from
new regressions without blanket skip markers in the suite.

- `tests.test_4109_notification_focus_existing_tab::test_test_notification_without_sid_still_targets_current_page_for_reuse`
- `tests.test_4109_notification_focus_existing_tab::test_service_worker_update_delivery_keeps_versioned_no_store_route`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[absent-unverified]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[failed-exit-unverified]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[interrupted-exit-unverified]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[active-active]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[dead-stale]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[over-age-stale]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[malformed-unknown]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[invalid-encoding-unknown]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[future-unknown]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[nonfinite-unknown]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[oversized-pid-unknown]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[dangling-unknown]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[unreadable-unknown]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[update-incomplete-incomplete]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[lazy-refresh-incomplete-incomplete]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[separate-venv-incomplete-incomplete]`
- `tests.test_agent_runtime_revision_guard::test_unverified_update_keeps_manual_409_without_restart[symlink-venv-incomplete-incomplete]`
- `tests.test_agent_runtime_revision_guard::test_async_compression_preserves_manual_restart_diagnostics[http-error]`
- `tests.test_agent_runtime_revision_guard::test_async_compression_preserves_manual_restart_diagnostics[raised-error]`
- `tests.test_agent_runtime_revision_guard::test_loaded_agent_runtime_fails_closed_after_source_revision_changes`
- `tests.test_auxiliary_models_settings.TestAuxiliaryModelsBackend::test_model_auxiliary_route_exists`
- `tests.test_extensions_settings_panel::test_switch_settings_section_supports_extensions_lazy_load`
- `tests.test_issue6611_regeneration_authority::test_snapshot_refuses_when_wal_data_version_changes`
- `tests.test_issue693_system_health_panel::test_system_health_route_registered_and_auth_gated`
- `tests.test_issue716_agent_heartbeat::test_agent_health_route_is_registered_with_tri_state_payload_shape`
- `tests.test_opencode_providers::test_opencode_zen_detected_via_env_key`
- `tests.test_opencode_providers::test_opencode_go_detected_via_env_key`
- `tests.test_opencode_providers::test_shared_opencode_api_key_detects_zen_and_go`
- `tests.test_plugins_panel.TestPluginsSettingsUi::test_plugins_panel_has_list_and_empty_state`
- `tests.test_provider_quota_status::test_settings_label_and_description_i18n_keys_exist_for_all_locales`
- `tests.test_pwa_manifest_sw.TestManifest::test_manifest_icons_reference_existing_files`
- `tests.test_pwa_manifest_sw.TestIndexHtmlIntegration::test_index_loads_pwa_startup_helper_early`
- `tests.test_pwa_manifest_sw.TestSessionManifestRoute::test_session_manifest_json_has_hermes_name`
- `tests.test_pwa_manifest_sw.TestSessionManifestRoute::test_session_manifest_webmanifest_is_parseable_json`
- `tests.test_pwa_manifest_sw.TestRootManifestRoute::test_root_manifest_json_has_hermes_name_and_512_icon`
- `tests.test_regressions::test_token_handler_guards_session_id`
- `tests.test_regressions::test_ui_js_can_upgrade_thinking_spinner_into_live_reasoning_card`
- `tests.test_sprint20::test_mic_button_inside_composer_left`
- `tests.test_sprint20::test_boot_js_prefers_server_side_stt_by_default`
- `tests.test_sprint20::test_boot_js_no_server_stt_first_click_uses_browser_speech_recognition`
- `tests.test_turn_duration_display::test_compact_worklog_hides_bottom_live_footer_timer`
- `tests.test_update_banner_fixes.TestConflictError::test_conflict_returns_conflict_flag`
- `tests.test_update_banner_fixes.TestConflictError::test_conflict_message_includes_git_command`
- `tests.test_update_banner_fixes.TestApplyUpdateRestartSafety::test_apply_update_refuses_when_stream_active`
- `tests.test_update_banner_fixes.TestApplyUpdateRestartSafety::test_force_update_refuses_when_stream_active`
- `tests.test_update_banner_fixes.TestApplyUpdateRestartSafety::test_apply_update_refuses_when_active_run_without_stream`
- `tests.test_update_banner_fixes.TestApplyUpdateRestartSafety::test_force_update_refuses_when_active_run_without_stream`
- `tests.test_update_banner_fixes.TestSuccessfulUpdateReturnsRestartScheduled::test_apply_update_returns_restart_scheduled`
- `tests.test_update_banner_fixes.TestSuccessfulUpdateReturnsRestartScheduled::test_apply_update_pulls_latest_release_tag_when_updates_are_release_based`
- `tests.test_update_banner_fixes.TestSuccessfulUpdateReturnsRestartScheduled::test_apply_update_falls_back_to_tracking_branch_without_release_tags`
- `tests.test_update_banner_fixes.TestApplyForceUpdate::test_apply_force_update_ok`
- `tests.test_update_banner_fixes.TestApplyForceUpdate::test_apply_force_update_proceeds_when_clean_fails`
- `tests.test_update_banner_fixes.TestAgentUpdateRequiresGatewayRestart::test_apply_update_agent_requires_gateway_restart`
- `tests.test_update_banner_fixes.TestAgentUpdateRequiresGatewayRestart::test_apply_update_agent_stash_conflict_success_invokes_gateway_restart`
- `tests.test_update_banner_fixes.TestAgentUpdateRequiresGatewayRestart::test_apply_update_agent_without_gateway_restart_result_fails`
- `tests.test_update_banner_fixes.TestAgentUpdateRequiresGatewayRestart::test_apply_force_update_agent_uses_gateway_restart_status`
- `tests.test_update_banner_fixes.TestAgentUpdateRequiresGatewayRestart::test_apply_force_update_agent_fails_when_gateway_restart_busy`
- `tests.test_update_banner_fixes.TestAgentUpdateRequiresGatewayRestart::test_apply_update_webui_does_not_call_gateway_restart`
- `tests.test_update_banner_fixes.TestUiJsUpdateBanner::test_success_toast_says_restarting`
- `tests.test_update_banner_fixes.TestUiJsUpdateBanner::test_reload_uses_health_poll_not_blind_timeout`
- `tests.test_update_banner_fixes.TestUiJsUpdateBanner::test_apply_and_force_updates_capture_identity`
- `tests.test_update_banner_fixes.TestUpdateBannerUx::test_update_banner_includes_release_labels`
- `tests.test_update_banner_fixes.TestUpdateBannerUx::test_settings_update_check_uses_same_repo_branch_formatter`
- `tests.test_update_banner_fixes.TestUpdateBannerUx::test_manual_webui_banner_hides_apply_button`
- `tests.test_update_banner_fixes.TestUpdateBannerUx::test_settings_manual_webui_update_includes_pull_guidance`
- `tests.test_update_banner_fixes.TestUpdateCompareSource::test_update_banner_frontend_uses_data_driven_compare_helpers`
- `tests.test_update_banner_fixes.TestUpdateCompareSource::test_update_banner_clears_stale_links_when_no_updates_remain`
- `tests.test_update_banner_fixes.TestUpdateCompareSource::test_manual_up_to_date_check_clears_update_banner`
- `tests.test_update_banner_fixes.TestForceButtonResetOnRetry::test_apply_updates_resets_force_button_at_start`
- `tests.test_workspace_stale_recovery::test_session_new_keeps_deleted_unverified_workspace_strict[explicit-request]`
- `tests.test_workspace_stale_recovery::test_session_new_keeps_deleted_unverified_workspace_strict[foreign-previous-session]`
- `tests.test_workspace_stale_recovery::test_session_new_keeps_deleted_unverified_workspace_strict[stored-workspace-mismatch]`


## Python 3.12 verification correction — 2026-10-04

The first full 3.12.14 run reported 14,422 passed, 757 failed, 285 skipped, 2 xfailed and 1 xpassed. Those failures have NOT all been proven upstream-only. Anchor-focused verification exposed five real runtime failures introduced by passing an unsupported dependency keyword to the scene-candidate matcher, and one source-location assertion still aimed at routes.py. Both call sites and the assertion owner were corrected; existing behavior tests provide regression coverage.

The supported local gate is now `bash scripts/mars-test.sh`; its manifest covers structural domains and anchor, workspace, media and authentication neighbors. This narrower gate does not reproduce the former 3,116-test comparison and must not be presented as full-product certification. Production was not deployed.

### Extraction lesson
- ❌ Inferring that all full-suite failures are inherited from a small green subset.
- ✅ Run the moved domain's behavior tests and compare the full baseline before classifying failures.
- 💡 Explicit dependency forwarding must match the callee signature at every facade and internal call site.
