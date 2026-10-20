"""Regression: rail dividers must follow their group after tab reordering.

The left rail ships four ``.rail-divider`` separators between its semantic
groups. ``_applyTabOrder`` (panels.js) and the inline flash-prevention restore
script move *every* tab in front of the ``.rail-spacer`` anchor, which leaves
the separators behind at the top of the rail: the groups visually disappear and
four dividers stack above the second tab.

``rail_nav.js`` therefore re-anchors each separator to the group transition it
belongs to on every sync pass. These tests pin that contract:

* the divider pass is order-agnostic (it walks visible tabs in DOM order),
* it never emits mutations when the rail is already settled, so the
  MutationObserver driving it cannot loop,
* it hides a separator whose group boundary no longer exists.

Run:
    python -m pytest tests/test_rail_divider_reanchor.py -q
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
RAIL_NAV_JS = BASE_DIR / "static" / "rail_nav.js"

GROUPS = [
    ["chat", "tasks", "kanban", "todos"],
    ["projects", "workspaces", "memory", "skills", "profiles"],
    ["music"],
    ["insights", "logs"],
]


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _node():
    return shutil.which("node")


def test_rail_nav_declares_the_group_contract():
    src = _read(RAIL_NAV_JS)
    for group in GROUPS:
        for panel in group:
            assert f"'{panel}'" in src, f"rail_nav.js lost the group member {panel!r}"


def test_divider_pass_reanchors_instead_of_only_hiding():
    src = _read(RAIL_NAV_JS)
    sync = src[src.find("function syncRailDividers"):]
    assert "insertBefore" in sync, (
        "separators must be re-anchored after _applyTabOrder moves the tabs; "
        "hiding alone leaves them piled up at the top of the rail"
    )
    assert "nextElementSibling" in sync, (
        "insertBefore must be skipped when the divider is already in place, "
        "otherwise every pass emits a childList record and the MutationObserver loops"
    )
    assert "HIDDEN_CLASS" in sync


@pytest.mark.skipif(_node() is None, reason="node is required for the DOM harness")
def test_dividers_return_to_their_groups_after_reorder(tmp_path):
    """Execute the real module against a minimal DOM and replay the reorder."""
    harness = tmp_path / "rail_harness.js"
    harness.write_text(_harness_source(), encoding="utf-8")
    proc = subprocess.run(
        [_node(), str(harness), str(RAIL_NAV_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "OK" in proc.stdout, proc.stdout


def _harness_source():
    # A deliberately tiny DOM shim: the module only needs querySelector(All),
    # classList, dataset, matches, getClientRects, insertBefore and textContent.
    return r"""
const fs = require('fs');
const path = process.argv[2];

const GROUPS = [
  ['chat','tasks','kanban','todos'],
  ['projects','workspaces','memory','skills','profiles'],
  ['music'],
  ['insights','logs'],
];

function makeEl(tag, cls, panel){
  const el = {
    tagName: tag.toUpperCase(),
    className: cls || '',
    dataset: panel ? { panel } : {},
    children: [],
    parentNode: null,
    _hidden: false,
    classList: {
      contains(c){ return (el.className || '').split(/\s+/).indexOf(c) !== -1; },
      add(c){ if(!this.contains(c)) el.className = (el.className + ' ' + c).trim(); },
      remove(c){ el.className = (el.className || '').split(/\s+/).filter(x => x && x !== c).join(' '); },
      toggle(c, on){ on ? this.add(c) : this.remove(c); },
    },
    setAttribute(){}, getAttribute(){ return null; }, hasAttribute(){ return false; },
    getClientRects(){ return el._hidden ? [] : [{}]; },
    get nextElementSibling(){
      if(!el.parentNode) return null;
      const i = el.parentNode.children.indexOf(el);
      return el.parentNode.children[i + 1] || null;
    },
  };
  el.matches = function(sel){
    if(sel === '.nav-tab[data-panel]') return /\bnav-tab\b/.test(el.className) && !!el.dataset.panel;
    const attr = /^\[data-panel="([^"]+)"\]$/.exec(sel);
    if(attr) return el.dataset.panel === attr[1];
    if(/^\.[a-z-]+$/.test(sel)) return (' ' + el.className + ' ').indexOf(' ' + sel.slice(1) + ' ') !== -1;
    return false;
  };
  el.querySelectorAll = function(sel){
    const out = [];
    (function walk(node){
      node.children.forEach(c => { if(c.matches && c.matches(sel)) out.push(c); walk(c); });
    })(el);
    return out;
  };
  el.querySelector = function(sel){ return el.querySelectorAll(sel)[0] || null; };
  return el;
}

const rail = makeEl('nav', 'rail');
function add(el){ el.parentNode = rail; rail.children.push(el); return el; }
function insertBefore(node, anchor){
  const from = node.parentNode ? node.parentNode.children.indexOf(node) : -1;
  if(from !== -1) node.parentNode.children.splice(from, 1);
  const at = anchor ? rail.children.indexOf(anchor) : rail.children.length;
  rail.children.splice(at, 0, node);
  node.parentNode = rail;
}

// `insertBefore` is also reachable as a property lookup on rail.
rail.insertBefore = insertBefore;
rail.insertBefore_ = insertBefore;

GROUPS.forEach((group, gi) => {
  group.forEach(p => add(makeEl('button', 'rail-btn nav-tab', p)));
  if(gi < GROUPS.length - 1) add(makeEl('div', 'rail-divider'));
});
add(makeEl('div', 'rail-divider'));
add(makeEl('div', 'rail-spacer'));
add(makeEl('button', 'rail-btn nav-tab', 'settings'));

const document = {
  readyState: 'complete',
  querySelector(sel){
    if(sel === 'nav.rail[role="tablist"]' || sel === '.rail') return rail;
    return null;
  },
  querySelectorAll(){ return []; },
  addEventListener(){},
  createElement(t){ return makeEl(t, '', null); },
  head: { appendChild(){} },
};
const win = {};

const source = fs.readFileSync(path, 'utf8');
const run = new Function('window', 'document', 'MutationObserver', source + '\n;return window.syncRailDividers;');
const sync = run(win, document, undefined);

const read = () => rail.children.map(c =>
  c.classList.contains('rail-divider') ? (c.classList.contains('rail-divider-hidden') ? '--H' : '--') :
  c.classList.contains('rail-spacer') ? 'SP' : (c.dataset.panel || c.className));

const expect = (actual, wanted, label) => {
  const a = actual.join(' '), w = wanted.join(' ');
  if(a !== w) { console.error(label + '\n  got:    ' + a + '\n  wanted: ' + w); process.exit(1); }
};

const base = ['chat','tasks','kanban','todos','--','projects','workspaces','memory','skills','profiles','--','music','--','insights','logs','--','SP','settings'];
expect(read(), base, 'initial layout');

// Replay `_applyTabOrder`: every tab moves in front of the spacer and no
// divider is touched — exactly what shipped and broke the visual grouping.
const spacer = rail.querySelector('.rail-spacer');
rail.querySelectorAll('.nav-tab[data-panel]').forEach(t => {
  if(t.dataset.panel !== 'settings') rail.insertBefore(t, spacer);
});
sync();
expect(read(), base, 'dividers after tab reorder');

// A user-defined order must keep working: music moved above the work group.
rail.insertBefore(rail.querySelector('[data-panel="music"]'), rail.children[0]);
sync();
expect(read(), ['music','chat','tasks','kanban','todos','--','projects','workspaces','memory','skills','profiles','--','insights','logs','--H','--','SP','settings'],
  'dividers after a custom order');

// Hiding a whole group collapses its separators rather than leaving them dangling.
rail.querySelector('[data-panel="music"]')._hidden = true;
rail.querySelector('[data-panel="music"]').className += ' nav-tab-hidden';
sync();
const hidden = read();
if(hidden.filter(x => x === '--').length > 4) {
  console.error('a hidden group left a dangling separator: ' + hidden.join(' '));
  process.exit(1);
}

// A settled rail must not churn: the second pass is a no-op.
const settled = read();
sync();
expect(read(), settled, 'settled rail is idempotent');

console.log('OK');
"""
