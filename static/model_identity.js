(function(root){
'use strict';
function parse(value, provider){
  var raw=String(value||'').trim(), lane=String(provider||'').trim();
  if(raw.startsWith('@custom:')){
    var rest=raw.slice(8), pos=rest.indexOf(':');
    if(pos>=0) return {model:rest.slice(pos+1),provider:'custom:'+rest.slice(0,pos)};
  }
  if(raw.startsWith('@')){
    var split=raw.indexOf(':');
    if(split>1) return {model:raw.slice(split+1),provider:raw.slice(1,split)};
  }
  return {model:raw,provider:lane};
}
function normalized(value){ return String(value||'').trim().toLowerCase().replace(/[\s._-]+/g,'-'); }
function findPin(overrides, value, provider){
  var target=parse(value,provider), keys=Object.keys(overrides||{});
  if(!target.model) return '';
  var qualified=keys.find(function(key){
    if(!key.startsWith('@')) return false;
    var identity=parse(key);
    return target.provider && identity.provider===target.provider && normalized(identity.model)===normalized(target.model);
  });
  var legacy=keys.find(function(key){return !key.startsWith('@')&&normalized(key)===normalized(target.model);});
  var hit=qualified||legacy;
  return hit?String(overrides[hit]||''):'';
}
var api={parse:parse,findPin:findPin};
root.MarsModelIdentity=api;
if(typeof module==='object'&&module.exports) module.exports=api;
})(typeof window!=='undefined'?window:globalThis);
