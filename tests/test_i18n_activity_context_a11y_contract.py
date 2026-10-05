"""Contracts for localized activity/status copy and context indicator UX.

The activity renderer and shell markup are intentionally out of scope here; these
source-level checks keep the shared i18n vocabulary and the existing accessibility
contract from regressing without modifying those protected files.
"""
from pathlib import Path
import json
import subprocess


def _runtime_translations():
    script = r'''
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync('static/i18n.js', 'utf8');
const ctx = { console, window: {}, document: { documentElement: {}, querySelectorAll: () => [] } };
vm.createContext(ctx);
vm.runInContext(source + ';globalThis.__locales = LOCALES;', ctx);
const keys = ['activity_status_running','activity_status_done','stream_status_error',
  'tool_status_error','thinking_status','activity_run_started','activity_model',
  'activity_status_announcement','context_usage_not_available'];
const out = {};
for (const locale of ['en','ru']) {
  ctx.__testLocale=locale;
  ctx.setLocale(locale);
  out[locale] = Object.fromEntries(keys.map(k => {
  const value = ctx.t(k, 'demo');
  return [k, value];
}));
}
process.stdout.write(JSON.stringify(out));
'''
    return json.loads(subprocess.check_output(['node', '-e', script], cwd=ROOT, text=True))

ROOT = Path(__file__).resolve().parents[1]
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
UI = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def test_activity_stream_tool_context_statuses_have_i18n_keys():
    locales = _runtime_translations()
    expected = {
        "en": {"activity_status_running": "Running", "activity_status_done": "Done",
               "stream_status_error": "Stream error", "tool_status_error": "Tool error",
               "thinking_status": "Thinking", "activity_run_started": "Run started",
               "activity_model": "Model: demo", "activity_status_announcement": "Agent is running",
               "context_usage_not_available": "Usage data is not available yet"},
        "ru": {"activity_status_running": "Выполняется", "activity_status_done": "Готово",
               "stream_status_error": "Ошибка потока", "tool_status_error": "Ошибка инструмента",
               "thinking_status": "Думаю", "activity_run_started": "Запуск начат",
               "activity_model": "Модель: demo", "activity_status_announcement": "Агент работает",
               "context_usage_not_available": "Данные об использовании пока недоступны"},
    }
    assert locales == expected


def test_context_indicator_never_invents_window_or_percentage():
    assert "128*1024" not in UI.replace(" ", "")
    assert "constctxWindow=Number(usage.context_length)>0?Number(usage.context_length):0" in UI.replace(" ", "")
    assert "hasPromptTok?String(pct)" in UI.replace(" ", "")


def test_context_indicator_requires_backend_usage_for_numeric_data():
    assert "consthasKnownContext=ctxWindow>0" in UI.replace(" ", "")
    assert "hasPromptTok=hasPromptTok&&hasKnownContext" in UI.replace(" ", "")
    assert "context_length" in UI


def test_activity_renderers_use_real_localized_interpolation():
    assert '" + t("activity_status_running") + "' not in UI
    assert '" + t("activity_tokens") + "' not in UI
    assert 'static/chat_activity.js' in (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_a11y_contract_has_disclosures_and_prompt_live_regions_but_no_token_stream_live_region():
    # Existing activity/tool disclosures and prompt dialogs must remain named.
    assert "tool-call-group-summary" in UI
    assert "aria-expanded" in UI
    # Clarification is a prompt surface; its existing dialog contract remains
    # in the protected message renderer and must stay named.
    assert "clarifyQuestion" in MESSAGES
    assert "aria-labelledby" in MESSAGES
    # Token streaming is deliberately not an aria-live region: announcing every
    # token makes screen readers unusable. Live announcements belong to status UI.
    for line in UI.splitlines():
        if "token" in line.lower() and "aria-live" in line.lower():
            raise AssertionError(f"aria-live attached to token stream: {line}")
