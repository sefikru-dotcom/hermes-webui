// Shared loading skeleton for side-panel lists (cron / skills / spaces / profiles).
// Every panel used to paint a bare "Loading..." text; a shimmer row that mirrors
// the real list anatomy reads as "content is on the way" instead of "something
// is stuck". Pure DOM builder — no app state, safe to call before boot.
// Usage: el.innerHTML = panelListSkeleton(3);  (rows: 1..20)
(function(){
  function panelListSkeleton(rows, opts){
    opts = opts || {};
    const count = Math.max(1, Math.min(20, Number(rows) || 3));
    const widths = opts.widths || [62, 48, 71, 55, 66, 44, 58];
    let html = '<div class="skeleton-list" role="status" aria-busy="true" aria-hidden="true">';
    for (let i = 0; i < count; i++) {
      const w = widths[i % widths.length];
      html += '<div class="skeleton-row" style="animation-delay:' + Math.min(i * 0.03, 0.2).toFixed(2) + 's">'
        + '<div class="skeleton-bar skeleton-title" style="width:' + w + '%"></div>'
        + '<div class="skeleton-bar skeleton-stamp"></div>'
        + '</div>';
    }
    return html + '</div>';
  }
  window.panelListSkeleton = panelListSkeleton;
})();
