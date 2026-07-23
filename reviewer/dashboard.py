"""Self-contained stats dashboard served at GET /dashboard.

No external assets (works from the container with no internet). Fetches
/stats from the same origin, forwarding the ?token= query param when present.
Palette/marks follow the validated reference dataviz palette (both modes).
"""

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MR Reviewer — Stats</title>
<style>
  :root {
    color-scheme: light;
    --page:      #f9f9f7;  --surface:  #fcfcfb;
    --ink:       #0b0b0b;  --ink-2:    #52514e;  --muted: #898781;
    --grid:      #e1e0d9;  --baseline: #c3c2b7;
    --border:    rgba(11,11,11,0.10);
    --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a;
    --s4: #eda100; --s5: #e87ba4; --s6: #008300;
    --seq: #2a78d6;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --page:      #0d0d0d;  --surface:  #1a1a19;
      --ink:       #ffffff;  --ink-2:    #c3c2b7;  --muted: #898781;
      --grid:      #2c2c2a;  --baseline: #383835;
      --border:    rgba(255,255,255,0.10);
      --s1: #3987e5; --s2: #d95926; --s3: #199e70;
      --s4: #c98500; --s5: #d55181; --s6: #008300;
      --seq: #3987e5;
    }
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    background: var(--page); color: var(--ink);
    font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 24px; max-width: 1080px; margin: 0 auto;
  }
  h1 { font-size: 18px; font-weight: 650; }
  .sub { color: var(--muted); font-size: 12px; margin: 2px 0 20px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
           gap: 12px; margin-bottom: 16px; }
  .tile { background: var(--surface); border: 1px solid var(--border);
          border-radius: 10px; padding: 14px 16px; }
  .tile .k { color: var(--ink-2); font-size: 12px; }
  .tile .v { font-size: 26px; font-weight: 650; margin-top: 2px; }
  .tile .d { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .card { background: var(--surface); border: 1px solid var(--border);
          border-radius: 10px; padding: 16px; margin-bottom: 16px; }
  .card h2 { font-size: 13px; font-weight: 650; color: var(--ink-2);
             margin-bottom: 12px; }
  svg text { font: 11px system-ui, -apple-system, "Segoe UI", sans-serif;
             fill: var(--muted); }
  .row { display: grid; grid-template-columns: minmax(120px, 220px) 1fr 70px;
         align-items: center; gap: 10px; padding: 3px 0; }
  .row .name { font-size: 12px; color: var(--ink);
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row .val { font-size: 12px; color: var(--ink-2); text-align: right;
              font-variant-numeric: tabular-nums; }
  .track { height: 14px; position: relative; }
  .track .bar { position: absolute; inset: 0 auto 0 0; border-radius: 0 4px 4px 0;
                min-width: 2px; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th { text-align: left; color: var(--muted); font-weight: 500;
       border-bottom: 1px solid var(--grid); padding: 6px 8px; }
  td { padding: 6px 8px; border-bottom: 1px solid var(--grid);
       font-variant-numeric: tabular-nums; }
  td.num { text-align: right; }
  tr:last-child td { border-bottom: none; }
  #tip { position: fixed; pointer-events: none; background: var(--surface);
         border: 1px solid var(--border); border-radius: 8px; padding: 6px 9px;
         font-size: 12px; box-shadow: 0 2px 10px rgba(0,0,0,.18);
         opacity: 0; transition: opacity .08s; z-index: 10; }
  #tip b { font-variant-numeric: tabular-nums; }
  .err { color: var(--ink-2); padding: 30px; text-align: center; }
</style>
</head>
<body>
  <h1>GitLab MR Reviewer — usage &amp; cost</h1>
  <div class="sub" id="updated">loading…</div>
  <div class="tiles" id="tiles"></div>
  <div class="card"><h2>Daily spend — last 30 days</h2>
    <svg id="daily" width="100%" height="150" role="img"
         aria-label="Daily spend, last 30 days"></svg></div>
  <div class="card"><h2>Spend by model</h2><div id="models"></div></div>
  <div class="card"><h2>Recent reviews</h2>
    <div style="overflow-x:auto"><table id="recent"></table></div></div>
  <div id="tip"></div>
<script>
(async function () {
  const qs = new URLSearchParams(location.search);
  const token = qs.get("token");
  const url = "/stats" + (token ? "?token=" + encodeURIComponent(token) : "");
  let data;
  try {
    const res = await fetch(url, {cache: "no-store"});
    if (!res.ok) throw new Error(res.status);
    data = await res.json();
  } catch (e) {
    document.body.innerHTML = '<p class="err">Cannot load /stats (' + e.message +
      '). If STATS_TOKEN is set, open /dashboard?token=&lt;token&gt;.</p>';
    return;
  }
  const fmt$ = v => "$" + v.toFixed(2);
  const fmtK = v => v >= 1e6 ? (v / 1e6).toFixed(1) + "M"
                  : v >= 1e3 ? (v / 1e3).toFixed(0) + "k" : String(v);
  const t = data.totals;
  document.getElementById("updated").textContent =
    "updated " + new Date().toLocaleString() + " · " + t.reviews + " reviews on record";

  // --- stat tiles ---
  const avg = t.reviews ? t.cost_usd / t.reviews : 0;
  const days = Object.keys(data.daily || {}).sort();
  const last7 = days.slice(-7).reduce((s, d) => s + data.daily[d].cost_usd, 0);
  document.getElementById("tiles").innerHTML = [
    ["Total spend", fmt$(t.cost_usd), t.input_tokens ? fmtK(t.input_tokens) + " in → " + fmtK(t.output_tokens) + " out" : ""],
    ["Reviews", String(t.reviews), ""],
    ["Avg / review", fmt$(avg), ""],
    ["Last 7 days", fmt$(last7), ""],
  ].map(([k, v, d]) =>
    '<div class="tile"><div class="k">' + k + '</div><div class="v">' + v +
    '</div><div class="d">' + d + '</div></div>').join("");

  // --- tooltip helpers ---
  const tip = document.getElementById("tip");
  const showTip = (ev, html) => { tip.innerHTML = html; tip.style.opacity = 1;
    tip.style.left = Math.min(ev.clientX + 12, innerWidth - 170) + "px";
    tip.style.top = (ev.clientY + 12) + "px"; };
  const hideTip = () => tip.style.opacity = 0;

  // --- daily bars (single series -> single hue, no legend) ---
  const svg = document.getElementById("daily");
  const W = svg.clientWidth || 900, H = 150, padB = 18, padT = 18;
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  const today = new Date();
  const range = [...Array(30)].map((_, i) => {
    const d = new Date(today); d.setUTCDate(d.getUTCDate() - (29 - i));
    return d.toISOString().slice(0, 10);
  });
  const vals = range.map(d => (data.daily && data.daily[d]) || {cost_usd: 0, reviews: 0});
  const max = Math.max(0.01, ...vals.map(v => v.cost_usd));
  const bw = Math.max(2, Math.floor(W / 30) - 2);
  let g = "";
  // recessive gridlines + baseline
  [0.5, 1].forEach(f => { const y = padT + (H - padB - padT) * (1 - f);
    g += '<line x1="0" x2="' + W + '" y1="' + y + '" y2="' + y +
         '" stroke="var(--grid)" stroke-width="1"/>' +
         '<text x="2" y="' + (y - 3) + '">' + fmt$(max * f) + "</text>"; });
  g += '<line x1="0" x2="' + W + '" y1="' + (H - padB) + '" y2="' + (H - padB) +
       '" stroke="var(--baseline)" stroke-width="1"/>';
  range.forEach((d, i) => {
    const v = vals[i], x = i * (W / 30) + 1;
    const h = Math.round((H - padB - padT) * (v.cost_usd / max));
    const y = H - padB - h;
    if (h > 0)
      g += '<rect data-i="' + i + '" x="' + x + '" y="' + y + '" width="' + bw +
           '" height="' + h + '" rx="2" fill="var(--seq)"/>';
    // oversized hit target for hover regardless of bar height
    g += '<rect data-i="' + i + '" x="' + x + '" y="0" width="' + bw +
         '" height="' + H + '" fill="transparent"/>';
    if (i % 7 === 1)
      g += '<text x="' + x + '" y="' + (H - 5) + '">' + d.slice(5) + "</text>";
  });
  svg.innerHTML = g;
  svg.addEventListener("mousemove", ev => {
    const el = ev.target.closest("[data-i]"); if (!el) return hideTip();
    const i = +el.dataset.i, v = vals[i];
    showTip(ev, range[i] + "<br><b>" + fmt$(v.cost_usd) + "</b> · " +
                v.reviews + " review" + (v.reviews === 1 ? "" : "s"));
  });
  svg.addEventListener("mouseleave", hideTip);

  // --- per-model horizontal bars (color follows entity, fixed slot order) ---
  const slots = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)"];
  const names = Object.keys(data.by_model || {}).sort();  // stable entity order
  const mmax = Math.max(1e-9, ...names.map(n => data.by_model[n].cost_usd));
  document.getElementById("models").innerHTML = names.map((n, i) => {
    const m = data.by_model[n];
    const w = Math.max(0.4, 100 * m.cost_usd / mmax);
    return '<div class="row" data-m="' + n + '"><div class="name">' + n +
      '</div><div class="track"><div class="bar" style="width:' + w +
      '%;background:' + slots[i % slots.length] + '"></div></div>' +
      '<div class="val">' + fmt$(m.cost_usd) + "</div></div>";
  }).join("") || '<div class="sub">no data yet</div>';
  document.getElementById("models").addEventListener("mousemove", ev => {
    const row = ev.target.closest("[data-m]"); if (!row) return hideTip();
    const m = data.by_model[row.dataset.m];
    showTip(ev, row.dataset.m + "<br><b>" + fmt$(m.cost_usd) + "</b> · " +
      m.calls + " calls · " + fmtK(m.input_tokens) + "→" + fmtK(m.output_tokens));
  });
  document.getElementById("models").addEventListener("mouseleave", hideTip);

  // --- recent reviews table ---
  const rows = (data.recent || []).slice()
    .sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
  document.getElementById("recent").innerHTML =
    "<tr><th>when (UTC)</th><th>project</th><th>MR</th>" +
    '<th class="num">in</th><th class="num">out</th><th class="num">cost</th></tr>' +
    rows.map(r =>
      "<tr><td>" + (r.ts || "").replace("T", " ").replace("Z", "") + "</td><td>" +
      (r.project || "?") + "</td><td>!" + r.mr_iid + '</td><td class="num">' +
      fmtK(r.input_tokens) + '</td><td class="num">' + fmtK(r.output_tokens) +
      '</td><td class="num">' + fmt$(r.cost_usd) + "</td></tr>").join("") ||
    "<tr><td class='sub'>no reviews yet</td></tr>";
})();
</script>
</body>
</html>
"""
