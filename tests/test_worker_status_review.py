import os
import subprocess
from pathlib import Path


def test_terminal_outcomes_and_late_events(tmp_path, monkeypatch):
    from api import worker_status as w
    monkeypatch.setattr(w, 'STATE_DIR', tmp_path)
    for outcome, expected in [('completed','completed'), ('timeout','failed'), ('interrupted','stopped'), ('mystery','unknown')]:
        w.update_worker_status(event_type='subagent.complete', child_session_id=outcome, status=outcome)
        assert w.statuses()[outcome]['status'] == expected
    w.update_worker_status(event_type='subagent.start', child_session_id='completed')
    w.update_worker_status(event_type='subagent.tool', child_session_id='completed')
    assert w.statuses()['completed']['status'] == 'completed'


def test_profile_isolation_and_restart(tmp_path, monkeypatch):
    from api import worker_status as w
    monkeypatch.setattr(w, 'STATE_DIR', tmp_path)
    w.update_worker_status(event_type='subagent.tool', child_session_id='a', profile='p1')
    assert not w.statuses('p2')
    rows = w.attach_worker_status([{'session_id':'a'}], 'p1')
    assert rows[0]['worker_status']['status'] == 'running'
    monkeypatch.setattr(w.os, 'getpid', lambda: -1)
    assert w.attach_worker_status([{'session_id':'a'}], 'p1')[0]['worker_status']['status'] == 'unknown'


def test_real_sidebar_label_function():
    source = (Path(__file__).parents[1] / 'static/sessions.js').read_text()
    code = source[source.index('const childWorkerStatus=(child)=>'):source.index('const installForkChildSwipe=', source.index('const childWorkerStatus=(child)=>'))]
    script = "const t=k=>({worker_status_running:'Работает',worker_status_completed:'Завершён',worker_status_failed:'Ошибка',worker_status_unknown:'Статус неизвестен'}[k]||k); const _sessionDisplayTitle=c=>c.title;const _sessionTimestampMs=c=>0;const _formatRelativeSessionTime=t=>'5m';\n" + code + "\nconst assert=require('assert'); assert(childLabelFor({worker_status:{status:'completed',goal:'Проверка',duration_seconds:87}}).includes('✓ Завершён · Проверка · выполнено за 1м 27с'));assert(childLabelFor({source:'subagent',title:'Subagent Session'}).includes('Статус неизвестен')); assert(!childLabelFor({source:'fork',title:'Fork'}).includes('неизвестен'));"
    subprocess.run(['node', '-e', script], check=True)
