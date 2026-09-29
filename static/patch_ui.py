import sys

path = 'static/ui.js'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Fix _transparentEventCountLabel
old_label = "return toolCount?`Trace: ${toolCount} ${toolCount===1?'tool':'tools'}`:'Trace';"
new_label = "return toolCount?t('activity_trace_count', toolCount):t('activity_trace_base');"
content = content.replace(old_label, new_label)

# 2. Fix Running labels
# Case 1: liveRunStatus content
content = content.replace('}<span class="lf-sep">·</span><span class="lf-status">Running</span>`;',
                        '} <span class="lf-sep">·</span> <span class="lf-status">" + t("activity_status_running") + "</span>`;')

# Case 2: _createActivityGroupInternal labels
content = content.replace('"tool-call-group-label tool-worklog-label as-text">Running</span>',
                        '"tool-call-group-label tool-worklog-label as-text">" + t("activity_status_running") + "</span>')
content = content.replace('"tool-call-group-label">Running</span>',
                        '"tool-call-group-label">" + t("activity_status_running") + "</span>')

# 3. Tokens label
content = content.replace('${_fmtTokens(tokens)} tokens</span>`',
                        '${_fmtTokens(tokens)} " + t("activity_tokens") + "</span>`')

# 4. Fix Activity: N tools
content = content.replace('label.textContent=`Activity: ${toolsPart}${suffix}`;',
                        'label.textContent=t("activity_count_tools", toolsPart) + suffix;')
content = content.replace('label.textContent=toolsPart?`Activity: ${toolsPart}${suffix}`:`Activity: ${suffix.slice(2)}`;',
                        'label.textContent=toolsPart?t("activity_count_tools", toolsPart)+suffix:t("activity_count_tools", "")+suffix.slice(2);')

# 5. Fix renderLiveAnchorActivityScene logic (normalization)
old_mode_known = "const knownMode=(m)=>m==='compact_worklog'||m==='transparent_stream'||m==='hide_all_activity';"
new_mode_known = "const knownMode=(m)=>m==='compact_worklog'||m==='transparent_stream'||m==='hide_all_activity'||m==='stream_full'||m==='stream_window'||m==='stream_quiet';"
content = content.replace(old_mode_known, new_mode_known)

# The core bug: normalization of new modes to legacy ones for the anchor engine
old_scene_logic = """const sceneMode=knownMode(activeMode)?activeMode:(knownMode(requestedMode)?requestedMode:activeMode);
  if(sceneMode==='hide_all_activity') return false;"""

new_scene_logic = """const _norm = (m) => {
    if(m === 'stream_full') return 'transparent_stream';
    if(m === 'stream_quiet') return 'hide_all_activity';
    if(m === 'stream_window') return 'compact_worklog';
    return m;
  };
  const sceneMode = _norm(knownMode(activeMode) ? activeMode : (knownMode(requestedMode) ? requestedMode : activeMode));
  if(sceneMode === 'hide_all_activity') return false;"""

content = content.replace(old_scene_logic, new_scene_logic)

# 6. Mismatched mode in _renderLiveAnchorActivitySceneForStream
old_for_stream = """  const mode=activeMode==='hide_all_activity'
    ? 'hide_all_activity'
    : (requestedMode==='compact_worklog'||requestedMode==='transparent_stream'||requestedMode==='hide_all_activity'
    ? requestedMode
    : activeMode);"""

new_for_stream = """  const _norm = (m) => {
    if(m === 'stream_full') return 'transparent_stream';
    if(m === 'stream_quiet') return 'hide_all_activity';
    if(m === 'stream_window') return 'compact_worklog';
    return m;
  };
  const mode = _norm(activeMode) === 'hide_all_activity'
    ? 'hide_all_activity'
    : (requestedMode === 'compact_worklog' || requestedMode === 'transparent_stream' || requestedMode === 'hide_all_activity'
    ? requestedMode
    : _norm(activeMode));"""

content = content.replace(old_for_stream, new_for_stream)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
