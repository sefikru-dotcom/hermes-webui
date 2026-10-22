/* mini_dom.mjs — a small, dependency-free DOM subset for behavioural harnesses.
 *
 * Why: the render/mode/scroll fixes must be verified by REAL behaviour (node identity
 * preserved, rows hidden not removed, one scroll write per frame), and the repository
 * has no jsdom. Installing one would add a network dependency to the verification path,
 * so this file implements exactly the surface the modules and the extracted ui.js
 * bridges touch. It is deliberately not a browser: no layout, no CSS, no events other
 * than plain addEventListener/dispatchEvent.
 *
 * Supported selectors: comma groups, descendant (' ') and child ('>') combinators,
 * tag / .class / #id / [attr] / [attr="v"] / [attr='v'] / :not(simple-compound).
 */
const ELEMENT_NODE = 1;
const TEXT_NODE = 3;
const VOID_TAGS = new Set(['br','hr','img','input','meta','link','source','area','base','col','embed','param','track','wbr']);

const ENTITIES = {amp:'&', lt:'<', gt:'>', quot:'"', apos:"'", nbsp:'\u00a0', '#39':"'", '#x27':"'"};

function decodeEntities(s){
  return String(s).replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (m, name) => {
    if(name[0]==='#'){
      const code = name[1]==='x'||name[1]==='X' ? parseInt(name.slice(2),16) : parseInt(name.slice(1),10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : m;
    }
    return Object.prototype.hasOwnProperty.call(ENTITIES,name) ? ENTITIES[name] : m;
  });
}

function escapeText(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

class ClassList {
  constructor(el){ this.el=el; }
  _list(){ return String(this.el.getAttribute('class')||'').split(/\s+/).filter(Boolean); }
  _write(list){ this.el.setAttribute('class', list.join(' ')); }
  add(...names){ const l=this._list(); for(const n of names) if(n && !l.includes(n)) l.push(n); this._write(l); }
  remove(...names){ this._write(this._list().filter(n=>!names.includes(n))); }
  contains(n){ return this._list().includes(n); }
  toggle(n, force){
    const has=this.contains(n);
    const want = force===undefined ? !has : !!force;
    if(want && !has) this.add(n);
    if(!want && has) this.remove(n);
    return want;
  }
}

class Style {
  constructor(){ this._props=new Map(); }
  setProperty(k,v){ this._props.set(String(k), String(v)); }
  getPropertyValue(k){ return this._props.get(String(k))||''; }
  removeProperty(k){ this._props.delete(String(k)); }
  get cssText(){ return [...this._props].map(([k,v])=>`${k}:${v}`).join(';'); }
  set cssText(v){ this._props.clear(); String(v||'').split(';').forEach(p=>{ const i=p.indexOf(':'); if(i>0) this._props.set(p.slice(0,i).trim(), p.slice(i+1).trim()); }); }
}
// `el.style.display = 'none'` must behave like the browser's camelCase accessors.
function makeStyle(){
  const s=new Style();
  return new Proxy(s, {
    get(t,p){
      if(p in t) return t[p];
      if(typeof p!=='string') return undefined;
      if(p==='cssText') return t.cssText;
      return t._props.get(p.replace(/[A-Z]/g,m=>'-'+m.toLowerCase()))||'';
    },
    set(t,p,v){
      if(p==='cssText'){ t.cssText=v; return true; }
      if(typeof p!=='string') return true;
      t._props.set(p.replace(/[A-Z]/g,m=>'-'+m.toLowerCase()), String(v));
      return true;
    }
  });
}

class Node {
  constructor(doc){ this.ownerDocument=doc; this.parentNode=null; this.childNodes=[]; }
  get nodeType(){ return this._nodeType||0; }
  get parentElement(){ return this.parentNode && this.parentNode.nodeType===ELEMENT_NODE ? this.parentNode : null; }
  get children(){ return this.childNodes.filter(n=>n.nodeType===ELEMENT_NODE); }
  get firstChild(){ return this.childNodes[0]||null; }
  get lastChild(){ return this.childNodes[this.childNodes.length-1]||null; }
  get firstElementChild(){ return this.children[0]||null; }
  get childElementCount(){ return this.children.length; }
  get nextSibling(){
    if(!this.parentNode) return null;
    const list=this.parentNode.childNodes;
    const i=list.indexOf(this);
    return i>=0?(list[i+1]||null):null;
  }
  get previousSibling(){
    if(!this.parentNode) return null;
    const list=this.parentNode.childNodes;
    const i=list.indexOf(this);
    return i>0?list[i-1]:null;
  }
  _detach(){
    if(!this.parentNode) return;
    const list=this.parentNode.childNodes;
    const i=list.indexOf(this);
    if(i>=0) list.splice(i,1);
    this.parentNode=null;
  }
  appendChild(node){
    if(node && node.nodeType===11 /* fragment */){
      for(const kid of [...node.childNodes]) this.appendChild(kid);
      return node;
    }
    node._detach();
    node.parentNode=this;
    this.childNodes.push(node);
    return node;
  }
  insertBefore(node, ref){
    if(ref===null||ref===undefined) return this.appendChild(node);
    if(node===ref) return node;
    if(node && node.nodeType===11){
      for(const kid of [...node.childNodes]) this.insertBefore(kid, ref);
      return node;
    }
    node._detach();
    const list=this.childNodes;
    const i=list.indexOf(ref);
    if(i<0) return this.appendChild(node);
    node.parentNode=this;
    list.splice(i,0,node);
    return node;
  }
  replaceChild(node, old){
    this.insertBefore(node, old);
    if(old.parentNode===this) this.removeChild(old);
    return old;
  }
  removeChild(node){
    const list=this.childNodes;
    const i=list.indexOf(node);
    if(i>=0){ list.splice(i,1); node.parentNode=null; }
    return node;
  }
  remove(){ this._detach(); }
  contains(node){
    let cur=node;
    while(cur){ if(cur===this) return true; cur=cur.parentNode; }
    return false;
  }
}

class TextNode extends Node {
  constructor(doc, text){ super(doc); this._nodeType=TEXT_NODE; this.data=String(text); }
  get textContent(){ return this.data; }
  set textContent(v){ this.data=String(v); }
  get outerHTML(){ return escapeText(this.data); }
  isEqualNode(other){
    return !!other && other.nodeType===TEXT_NODE && other.data===this.data;
  }
}

function parseSelector(selector){
  // -> [{combinator:' '|'>', compound:parts[]}]
  const groups=String(selector||'').split(',').map(s=>s.trim()).filter(Boolean);
  return groups.map(group=>{
    const tokens=[];
    const re=/([>])\s*|(\s+)/g;
    let rest=group;
    // Normalise combinators into an explicit token stream.
    const parts=[];
    let buf='';
    let pendingComb='';
    let depth=0;
    for(let i=0;i<rest.length;i++){
      const ch=rest[i];
      if(ch==='(') depth++;
      if(ch===')') depth--;
      if(depth===0 && ch==='>'){ if(buf.trim()) parts.push({comb:pendingComb,sel:buf.trim()}); pendingComb='>'; buf=''; continue; }
      if(depth===0 && /\s/.test(ch)){
        if(buf.trim()){ parts.push({comb:pendingComb,sel:buf.trim()}); pendingComb=' '; buf=''; }
        continue;
      }
      buf+=ch;
    }
    if(buf.trim()) parts.push({comb:pendingComb,sel:buf.trim()});
    if(parts.length) parts[0].comb='';
    return parts;
  });
}

function matchCompound(el, compound){
  if(!el || el.nodeType!==ELEMENT_NODE) return false;
  // split into tag / #id / .class / [attr...] / :not(...)
  let rest=compound;
  const nots=[];
  rest=rest.replace(/:not\(([^)]*)\)/g,(m,inner)=>{ nots.push(inner.trim()); return ''; });
  const tagMatch=rest.match(/^[a-zA-Z][\w-]*/);
  if(tagMatch){
    if(el.tagName.toLowerCase()!==tagMatch[0].toLowerCase()) return false;
    rest=rest.slice(tagMatch[0].length);
  }
  const idMatches=[...rest.matchAll(/#([\w-]+)/g)].map(m=>m[1]);
  for(const id of idMatches) if(el.getAttribute('id')!==id) return false;
  const classMatches=[...rest.matchAll(/\.([\w-]+)/g)].map(m=>m[1]);
  for(const c of classMatches) if(!el.classList.contains(c)) return false;
  const attrMatches=[...rest.matchAll(/\[([\w:-]+)(?:([~^$*|]?=)["']?([^\]"']*)["']?)?\]/g)];
  for(const [,name,op,val] of attrMatches){
    const have=el.getAttribute(name);
    if(have===null||have===undefined) return false;
    if(!op) continue;
    if(op==='=' && have!==val) return false;
    if(op==='*=' && !String(have).includes(val)) return false;
    if(op==='^=' && !String(have).startsWith(val)) return false;
    if(op==='$=' && !String(have).endsWith(val)) return false;
    if(op==='~=' && !String(have).split(/\s+/).includes(val)) return false;
  }
  for(const inner of nots){
    if(matchCompound(el, inner)) return false;
  }
  return true;
}

function matchesParts(el, parts, index){
  const part=parts[index];
  if(!matchCompound(el, part.sel)) return false;
  if(index===0) return true;
  const comb=part.comb||' ';
  if(comb==='>'){
    const parent=el.parentElement;
    return !!parent && matchesParts(parent, parts, index-1);
  }
  let cur=el.parentElement;
  while(cur){
    if(matchesParts(cur, parts, index-1)) return true;
    cur=cur.parentElement;
  }
  return false;
}

class Element extends Node {
  constructor(doc, tagName){
    super(doc);
    this._nodeType=ELEMENT_NODE;
    this.tagName=String(tagName||'div').toUpperCase();
    this._attrs=new Map();
    this.classList=new ClassList(this);
    this.style=makeStyle();
    this._listeners=new Map();
    this.scrollTop=0;
    this.scrollHeight=0;
    this.clientHeight=0;
    this.clientWidth=0;
    this.offsetX=0;
  }
  get nodeName(){ return this.tagName; }
  get id(){ return this.getAttribute('id')||''; }
  set id(v){ this.setAttribute('id', v); }
  get className(){ return this.getAttribute('class')||''; }
  set className(v){ this.setAttribute('class', v); }
  get hidden(){ return this.hasAttribute('hidden'); }
  set hidden(v){ if(v) this.setAttribute('hidden',''); else this.removeAttribute('hidden'); }
  get dataset(){
    const el=this;
    return new Proxy({}, {
      get(_t,prop){
        if(typeof prop!=='string') return undefined;
        const attr='data-'+prop.replace(/[A-Z]/g,m=>'-'+m.toLowerCase());
        return el.getAttribute(attr);
      },
      set(_t,prop,value){ el.setAttribute('data-'+String(prop).replace(/[A-Z]/g,m=>'-'+m.toLowerCase()), value); return true; },
      has(_t,prop){ return el.hasAttribute('data-'+String(prop).replace(/[A-Z]/g,m=>'-'+m.toLowerCase())); },
      deleteProperty(_t,prop){ el.removeAttribute('data-'+String(prop).replace(/[A-Z]/g,m=>'-'+m.toLowerCase())); return true; }
    });
  }
  getAttribute(name){
    const v=this._attrs.get(String(name).toLowerCase());
    return v===undefined?null:v;
  }
  setAttribute(name, value){
    this._attrs.set(String(name).toLowerCase(), String(value));
  }
  hasAttribute(name){ return this._attrs.has(String(name).toLowerCase()); }
  removeAttribute(name){ this._attrs.delete(String(name).toLowerCase()); }
  getAttributeNames(){ return [...this._attrs.keys()]; }
  get attributes(){
    return [...this._attrs].map(([name,value])=>({name,value}));
  }
  get textContent(){
    return this.childNodes.map(n=>n.textContent).join('');
  }
  set textContent(v){
    for(const n of [...this.childNodes]) n._detach();
    if(v!==''&&v!==null&&v!==undefined) this.appendChild(new TextNode(this.ownerDocument, v));
  }
  get innerHTML(){ return this.childNodes.map(n=>n.outerHTML).join(''); }
  set innerHTML(html){
    for(const n of [...this.childNodes]) n._detach();
    const nodes=parseHTML(String(html||''), this.ownerDocument);
    for(const n of nodes) this.appendChild(n);
  }
  get outerHTML(){
    const attrs=[...this._attrs].map(([k,v])=>` ${k}="${String(v).replace(/"/g,'&quot;')}"`).join('');
    const tag=this.tagName.toLowerCase();
    return `<${tag}${attrs}>${this.innerHTML}</${tag}>`;
  }
  isEqualNode(other){
    if(!other||other.nodeType!==ELEMENT_NODE) return false;
    if(other.tagName!==this.tagName) return false;
    if(this._attrs.size!==other._attrs.size) return false;
    for(const [k,v] of this._attrs) if(other.getAttribute(k)!==v) return false;
    const a=this.childNodes, b=other.childNodes;
    if(a.length!==b.length) return false;
    for(let i=0;i<a.length;i++) if(!a[i].isEqualNode(b[i])) return false;
    return true;
  }
  matches(selector){
    const groups=parseSelector(selector);
    return groups.some(parts=>matchesParts(this, parts, parts.length-1));
  }
  closest(selector){
    let cur=this;
    while(cur&&cur.nodeType===ELEMENT_NODE){
      if(cur.matches(selector)) return cur;
      cur=cur.parentElement;
    }
    return null;
  }
  _walk(out){
    for(const kid of this.childNodes){
      if(kid.nodeType===ELEMENT_NODE){ out.push(kid); kid._walk(out); }
    }
    return out;
  }
  querySelectorAll(selector){
    const groups=parseSelector(selector);
    const all=this._walk([]);
    return all.filter(el=>groups.some(parts=>matchesParts(el, parts, parts.length-1)));
  }
  querySelector(selector){
    const list=this.querySelectorAll(selector);
    return list.length?list[0]:null;
  }
  addEventListener(type, fn){ if(!this._listeners.has(type)) this._listeners.set(type,[]); this._listeners.get(type).push(fn); }
  removeEventListener(type, fn){
    const list=this._listeners.get(type);
    if(!list) return;
    this._listeners.set(type, list.filter(f=>f!==fn));
  }
  dispatchEvent(evt){
    const type=evt&&evt.type;
    const list=this._listeners.get(type)||[];
    for(const fn of [...list]) fn.call(this, evt);
    return true;
  }
  insertAdjacentElement(position, node){
    if(position==='afterend'&&this.parentNode) this.parentNode.insertBefore(node, this.nextSibling);
    else if(position==='beforebegin'&&this.parentNode) this.parentNode.insertBefore(node, this);
    else if(position==='afterbegin') this.insertBefore(node, this.firstChild);
    else this.appendChild(node);
    return node;
  }
  getBoundingClientRect(){ return {top:0,left:0,width:0,height:0,bottom:0,right:0}; }
  getBoundingClientRectAsync(){ return Promise.resolve(this.getBoundingClientRect()); }
  focus(){}
  blur(){}
  click(){ this.dispatchEvent({type:'click',target:this}); }
  get offsetHeight(){ return this.clientHeight; }
}

class Fragment extends Node {
  constructor(doc){ super(doc); this._nodeType=11; }
  get outerHTML(){ return this.childNodes.map(n=>n.outerHTML).join(''); }
  isEqualNode(other){
    if(!other||other.nodeType!==11) return false;
    const a=this.childNodes, b=other.childNodes;
    if(a.length!==b.length) return false;
    for(let i=0;i<a.length;i++) if(!a[i].isEqualNode(b[i])) return false;
    return true;
  }
}

/* Very small HTML parser: tags, quoted/unquoted attributes, text, void elements.
 * Comments are dropped; script/style content is treated as text (not needed here). */
function parseHTML(html, doc){
  const root=new Fragment(doc);
  const stack=[root];
  const re=/<!--[\s\S]*?-->|<\/?([a-zA-Z][\w:-]*)((?:\s+[^\s"'>/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+))?)*)\s*(\/?)>/g;
  let last=0;
  let m;
  const pushText=(text)=>{
    if(!text) return;
    stack[stack.length-1].appendChild(new TextNode(doc, decodeEntities(text)));
  };
  while((m=re.exec(html))){
    if(m.index>last) pushText(html.slice(last, m.index));
    last=re.lastIndex;
    if(m[0].startsWith('<!--')) continue;
    const closing=m[0][1]==='/';
    const tag=m[1].toLowerCase();
    if(closing){
      for(let i=stack.length-1;i>0;i--){
        if(stack[i].tagName.toLowerCase()===tag){ stack.length=i; break; }
      }
      continue;
    }
    const el=doc.createElement(tag);
    const attrRe=/([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
    let am;
    while((am=attrRe.exec(m[2]||''))){
      const name=am[1];
      if(!name||name==='/') continue;
      const value=am[2]!==undefined?am[2]:(am[3]!==undefined?am[3]:(am[4]!==undefined?am[4]:''));
      el.setAttribute(name, decodeEntities(value));
    }
    stack[stack.length-1].appendChild(el);
    const selfClosing=m[3]==='/'||VOID_TAGS.has(tag);
    if(!selfClosing) stack.push(el);
  }
  if(last<html.length) pushText(html.slice(last));
  return [...root.childNodes];
}

export function createDocument(){
  const doc={
    nodeType:9,
    _byId:new Map(),
    createElement(tag){ return new Element(doc, tag); },
    createTextNode(text){ return new TextNode(doc, text); },
    createDocumentFragment(){ return new Fragment(doc); },
    addEventListener(){},
    removeEventListener(){}
  };
  const html=doc.createElement('html');
  const body=doc.createElement('body');
  html.appendChild(body);
  doc.documentElement=html;
  doc.body=body;
  doc.head=doc.createElement('head');
  html.insertBefore(doc.head, body);
  doc.getElementById=(id)=>{
    const found=html.querySelector('#'+String(id).replace(/[^\w-]/g,''));
    return found||null;
  };
  doc.querySelector=(sel)=>html.querySelector(sel);
  doc.querySelectorAll=(sel)=>html.querySelectorAll(sel);
  return doc;
}

export function installGlobals(doc){
  globalThis.document=doc;
  globalThis.window=globalThis;
  globalThis.Node={ELEMENT_NODE,TEXT_NODE};
  if(!globalThis.performance) globalThis.performance={now:()=>Date.now()};
  return doc;
}

export { Element, TextNode, Fragment, parseHTML, matchCompound };
