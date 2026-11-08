"""The single page the local server hands the browser.

Kept as one string with no build step and no CDN, so the repo stays
clone-and-run. All data arrives from /api/state as JSON; changing a setting
re-queries that endpoint, which does not touch the network.
"""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>India IPO Engine</title>
<style>
:root{
  --bg:#f6f7f9; --panel:#fff; --ink:#15181d; --muted:#6b7280; --line:#e4e7ec;
  --accent:#1f6feb; --pos:#0f7b3f; --posbg:#e7f6ed; --neg:#b4232c;
  --negbg:#fdeced; --warn:#9a6700; --warnbg:#fff8e1;
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#0f1115; --panel:#171a21; --ink:#e8eaed; --muted:#9aa3af; --line:#262b35;
  --accent:#5b9dff; --pos:#4ade80; --posbg:#10291b; --neg:#f87171;
  --negbg:#2b1416; --warn:#fbbf24; --warnbg:#2a2110;}}
*{box-sizing:border-box}
body{margin:0;padding:20px 16px 60px;background:var(--bg);color:var(--ink);
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1240px;margin:0 auto}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;
 justify-content:space-between;margin-bottom:6px}
h1{font-size:21px;margin:0;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:12.5px;margin-bottom:18px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
 margin-bottom:18px;overflow:hidden}
.panel h2{font-size:12px;margin:0;padding:12px 16px;border-bottom:1px solid var(--line);
 text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.body{padding:14px 16px}
.controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}
label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
 color:var(--muted);margin-bottom:5px}
input[type=number]{width:100%;padding:8px 10px;border:1px solid var(--line);
 border-radius:7px;background:var(--bg);color:var(--ink);font-size:15px}
input[type=range]{width:100%;accent-color:var(--accent)}
button{padding:8px 14px;border:1px solid var(--line);border-radius:7px;
 background:var(--accent);color:#fff;font-size:14px;font-weight:600;cursor:pointer}
button.ghost{background:var(--panel);color:var(--ink)}
button:disabled{opacity:.55;cursor:default}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.card{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:12px}
.card .n{font-size:23px;font-weight:650;letter-spacing:-.02em}
.card .l{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{padding:9px 11px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
 background:var(--bg);font-weight:600}
th:first-child,td:first-child{text-align:left;white-space:normal;min-width:180px}
tbody tr:last-child td{border-bottom:none}
tbody tr.open{background:var(--posbg)}
.name{font-weight:600}
.meta{color:var(--muted);font-size:11px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:10.5px;font-weight:650}
.HIGH,.STRONG{background:var(--posbg);color:var(--pos)}
.MODERATE,.ACTIONABLE{background:var(--warnbg);color:var(--warn)}
.LOW,.WATCH{background:var(--negbg);color:var(--neg)}
.VERYLOW,.AVOID{background:var(--negbg);color:var(--neg);opacity:.8}
.UNKNOWN,.NODATA{background:var(--bg);color:var(--muted)}
.pos{color:var(--pos);font-weight:600}.neg{color:var(--neg);font-weight:600}
.warn{color:var(--warn);font-weight:600}.thin{color:var(--muted)}
.bar{height:5px;border-radius:3px;background:var(--line);min-width:50px;margin-top:4px}
.bar i{display:block;height:100%;background:var(--accent);border-radius:3px}
.note{background:var(--warnbg);border-left:3px solid var(--warn);border-radius:8px;
 padding:12px 14px;font-size:12.5px;line-height:1.55}
.empty{padding:24px;text-align:center;color:var(--muted);font-size:13.5px}
.preview{margin:14px 16px 0;padding:10px 13px;border-radius:8px;font-size:12.5px;
 line-height:1.5;background:var(--warnbg);border-left:3px solid var(--warn)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
@media (max-width:640px){body{padding:14px 10px 40px}.card .n{font-size:19px}}
</style></head><body><div class="wrap">

<header>
  <h1>India IPO Engine</h1>
  <div class="row">
    <span class="meta" id="stamp"></span>
    <button id="refresh" class="ghost">Refresh live data</button>
  </div>
</header>
<div class="sub" id="modelline"></div>

<div class="panel"><h2>Your settings</h2><div class="body">
  <div class="controls">
    <div><label>Goal — listing gain you want</label>
      <input type="number" id="goal" value="10" min="1" max="100" step="1"></div>
    <div><label>Confidence — <span id="conflabel">60%</span></label>
      <input type="range" id="confidence" value="60" min="10" max="95" step="5"></div>
    <div><label>Capital you can block (₹)</label>
      <input type="number" id="capital" value="100000" min="10000" step="10000"></div>
    <div><label>PANs you can fund</label>
      <input type="number" id="pans" value="6" min="1" max="20" step="1"></div>
  </div>
  <div class="meta" style="margin-top:10px">
    Changing these is instant — no refetch. One lot per PAN: extra lots do not
    improve allotment odds.
  </div>
</div></div>

<div class="panel"><h2>Market regime</h2><div class="body" id="regime"></div></div>
<div class="panel"><h2>Your plan</h2><div id="plan"></div></div>
<div class="panel"><h2>Issues</h2><div class="scroll" id="issues"></div></div>

<div class="note">
<b>Read this before acting.</b> Odds come from how ~30 similar past issues
actually listed, across a single 18-month stretch of one market — if
conditions turn, those neighbours stop being representative. Coverage is
mainboard-heavy; SME is barely represented. The estimated gain also uses
<b>Grey Market Premium</b>, an unofficial, unregulated quote that can be thin
or manipulated. A high subscription multiple and a good allotment chance are
<i>opposites</i>. Informational software, not investment advice.
</div>
</div>

<script>
const $ = id => document.getElementById(id);
const inr = v => v == null ? "—" :
  Math.abs(v) >= 1e7 ? "₹" + (v/1e7).toFixed(2) + " Cr" :
  Math.abs(v) >= 1e5 ? "₹" + (v/1e5).toFixed(2) + " L" :
  "₹" + Math.round(v).toLocaleString("en-IN");
const pct = (v, d=1) => v == null ? '<span class="thin">—</span>'
  : `<span class="${v>0?'pos':v<0?'neg':'thin'}">${v>0?'+':''}${v.toFixed(d)}%</span>`;
const sub = v => v == null ? '<span class="thin">—</span>'
  : `<span class="${v>=1?'pos':'neg'}">${v.toFixed(2)}x</span>`;
const esc = s => (s??"").replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function odds(i){
  if (i.probability == null) return '<span class="thin">—</span>';
  const p = i.probability*100;
  const cls = p>=60?'pos':p>=30?'warn':'neg';
  const spread = (i.p25!=null && i.p75!=null)
    ? `<div class="meta">similar: ${i.p25>0?'+':''}${i.p25.toFixed(0)}% to ${i.p75>0?'+':''}${i.p75.toFixed(0)}%</div>` : '';
  return `<span class="${cls}" style="font-weight:650">${p.toFixed(0)}%</span>${spread}`;
}

function allot(v){
  if (v == null) return '<span class="thin">—</span>';
  return `${(v*100).toFixed(2)}%<div class="bar"><i style="width:${Math.max(1.5,Math.min(100,v*100))}%"></i></div>`;
}

function render(d){
  $("stamp").textContent = d.fetched_at ? "Updated " + d.fetched_at : "";
  $("modelline").textContent = d.model_ready
    ? `Odds from ${d.history} realised listings · goal ${d.settings.goal}% · confidence ${(d.settings.confidence*100).toFixed(0)}%`
    : `Only ${d.history} realised listings — not enough to quote odds yet, showing estimates instead.`;

  const r = d.regime;
  $("regime").innerHTML = r.sample ? `<div class="cards">
    <div class="card"><div class="n"><span class="pill ${r.label}">${r.label}</span></div><div class="l">Regime</div></div>
    <div class="card"><div class="n">${r.median>0?'+':''}${r.median.toFixed(1)}%</div><div class="l">Median listing gain</div></div>
    <div class="card"><div class="n">${(r.hit_rate*100).toFixed(0)}%</div><div class="l">Cleared your goal</div></div>
    <div class="card"><div class="n">${(r.negative_rate*100).toFixed(0)}%</div><div class="l">Listed below issue</div></div>
    <div class="card"><div class="n">${r.sample}</div><div class="l">Recent listings</div></div></div>`
    : '<div class="empty">No listing history yet.</div>';

  const p = d.plan;
  const banner = p.preview
    ? `<div class="preview">No IPO is open today — Indian IPOs arrive in bursts.
       This is a <b>preview</b> of what your settings would do against the last
       batch of closed issues, so you can see them working. Not actionable.</div>`
    : "";
  const rows = p.rows.map(a => `<tr><td><div class="name">${esc(a.name)}</div></td>
    <td>${a.pans}<div class="meta ${a.limited_by==='capital'?'warn':''}">${esc(a.limit_note)}</div></td>
    <td>${inr(a.lot)}</td><td>${inr(a.capital)}</td>
    <td>${(a.odds*100).toFixed(2)}%</td><td>${pct(a.gain)}</td><td>${inr(a.ev)}</td></tr>`).join("");
  const skipped = p.skipped.length
    ? `<div class="meta" style="padding:0 16px 12px">Out of reach on this budget: ` +
      p.skipped.map(s => `${esc(s.name)} (${inr(s.lot)})`).join("; ") + `</div>` : "";
  $("plan").innerHTML = p.rows.length ? `${banner}<div class="scroll"><table>
    <thead><tr><th>Issue</th><th>PANs</th><th>1 lot</th><th>Capital</th>
    <th>Allotment odds</th><th>Est. gain</th><th>E[value]</th></tr></thead>
    <tbody>${rows}</tbody></table></div>
    <div class="body"><div class="cards">
      <div class="card"><div class="n">${(p.p_none*100).toFixed(0)}%</div><div class="l">No allotment at all</div></div>
      <div class="card"><div class="n">${(p.p_any*100).toFixed(0)}%</div><div class="l">One or more</div></div>
      <div class="card"><div class="n">${p.expected_allotments.toFixed(2)}</div><div class="l">Expected allotments</div></div>
      <div class="card"><div class="n">${inr(p.expected_inr)}</div><div class="l">Expected value</div></div>
      <div class="card"><div class="n">${p.applications}</div><div class="l">Applications</div></div>
    </div><div class="meta" style="margin-top:10px">Deploys ${inr(p.deployed)}, leaves ${inr(p.idle)} idle.
      ${p.rows.some(a => a.limited_by === 'capital')
        ? '<b>Capital is the limit here, not PANs</b> — adding PANs changes nothing until the budget covers another lot.'
        : 'Your PANs are the limit — more capital changes nothing until another issue opens.'}</div></div>${skipped}`
    : `${banner}<div class="empty">Nothing clears your settings at this goal and confidence.<br>
       <span class="meta">Try lowering the goal or the confidence to see what would qualify.</span></div>${skipped}`;

  const body = d.issues.map(i => `<tr class="${i.open?'open':''}">
    <td><div class="name">${esc(i.name)}</div><div class="meta">${esc(i.exchange||i.segment)}${i.close_date?' · closes '+i.close_date:''}${i.open?' · OPEN':''}</div></td>
    <td><span class="pill ${(i.band||'UNKNOWN').replace(/\s/g,'')}">${i.band||'UNKNOWN'}</span></td>
    <td>${odds(i)}</td><td>${pct(i.expected_gain)}</td><td>${pct(i.gmp_pct)}</td>
    <td>${sub(i.total_sub)}</td><td>${sub(i.qib_sub)}</td>
    <td class="thin">${i.weakest||'—'}</td><td>${allot(i.allotment)}</td>
    <td>${inr(i.lot_value)}</td><td>${i.ev==null?'—':inr(i.ev)}</td></tr>`).join("");
  $("issues").innerHTML = d.issues.length ? `<table><thead><tr>
    <th>Issue</th><th>Odds</th><th>Chance of goal</th><th>Est. listing</th><th>GMP</th>
    <th>Total sub</th><th>QIB</th><th>Weakest leg</th><th>Allotment odds</th>
    <th>Capital / lot</th><th>E[value]</th></tr></thead><tbody>${body}</tbody></table>`
    : '<div class="empty">No issues tracked.</div>';
}

async function load(){
  const q = new URLSearchParams({
    goal: $("goal").value,
    confidence: (+$("confidence").value / 100).toFixed(2),
    capital: $("capital").value,
    pans: $("pans").value});
  const res = await fetch("/api/state?" + q);
  render(await res.json());
}

["goal","capital","pans"].forEach(id => $(id).addEventListener("change", load));
$("confidence").addEventListener("input", () => {
  $("conflabel").textContent = $("confidence").value + "%"; });
$("confidence").addEventListener("change", load);
$("refresh").addEventListener("click", async () => {
  const b = $("refresh");
  b.disabled = true; b.textContent = "Fetching…";
  await fetch("/api/refresh");
  const poll = setInterval(async () => {
    const res = await fetch("/api/state");
    const d = await res.json();
    if (!d.refreshing){
      clearInterval(poll);
      b.disabled = false; b.textContent = "Refresh live data";
      load();
    }
  }, 2000);
});
load();
</script></body></html>
"""
