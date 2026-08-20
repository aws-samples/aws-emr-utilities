"""EMR Test Drive — report renderer (preview implementation).

Emits one self-contained HTML file: no CDN, no JavaScript framework, no server.
Charts are inline SVG generated here; interactivity is ~40 lines of vanilla JS.
The file is mailable and works offline, which matters because the audience for
an upgrade decision is often someone who will never open a terminal.

Also emits report.json — the same content, machine-readable, for CI gating.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from .compare import STATE_LABEL, usd

# ------------------------------------------------------------------- palette

VERDICT_STYLE = {
    "BLOCK": ("crit", "Do not upgrade yet"),
    "CAUTION": ("warn", "Proceed with caution"),
    "PROCEED": ("ok", "Safe to proceed"),
    "INDETERMINATE": ("info", "Observation only — no verdict"),
}

CHIP = {
    "NEW_FAILURE": "crit", "STABLE_FAIL": "warn", "FLAKY": "warn",
    "EXPECTED_REMOVED": "warn", "FIXED": "ok", "FIXED_BY_RELEASE": "ok",
    "STABLE_PASS": "neutral", "EXPECTED_UNSUPPORTED": "info",
    "NOT_COMPARABLE": "muted", "MISSING": "muted",
    "REGRESSION": "crit", "IMPROVEMENT": "ok", "NEUTRAL": "neutral", "WITHIN_NOISE": "muted", "OVERHEAD": "info",
    "NEW_TIMEOUT": "crit", "RESOLVED_TIMEOUT": "ok",
    "INSUFFICIENT_DATA": "muted", "NO_DATA": "muted",
    "SILENT_DATA_LOSS": "crit", "DIVERGENT_RESULT": "crit", "ORPHANED_DATA": "warn",
    "CORRECTNESS_FIXED": "ok",
    "MATCHED": "ok", "UNMATCHED": "warn", "UNMATCHED_BY_DESIGN": "info",
}

HEAT = {
    "STABLE_PASS": "#037f0c", "FIXED": "#2bb534", "FIXED_BY_RELEASE": "#2bb534",
    "NEW_FAILURE": "#d91515", "STABLE_FAIL": "#f0895d", "FLAKY": "#d9a415",
    "EXPECTED_UNSUPPORTED": "#8994a3", "EXPECTED_REMOVED": "#c76d0a",
    "NOT_COMPARABLE": "#dfe3e8", "MISSING": "#dfe3e8",
}

# A product badge, not a reproduction of the AWS logo. Hand-drawing the wordmark
# looked wrong at every size, so this is an honest monogram in AWS accent orange.
PRODUCT_BADGE = (
    '<svg class="badge" viewBox="0 0 44 44" width="42" height="42" role="img" '
    'aria-label="EMR Test Drive">'
    '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#ff9900"/><stop offset="1" stop-color="#e07b00"/>'
    "</linearGradient></defs>"
    '<rect x="0" y="0" width="44" height="44" rx="10" fill="url(#bg)"/>'
    '<text x="22" y="27" text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,'
    'Helvetica,Arial,sans-serif" font-size="14" font-weight="700" fill="#16191f" '
    'letter-spacing="-.3">EMR</text>'
    "</svg>")


def access_label(mode: str) -> str:
    """PLAIN / LF-FTA / LF-FGAC — one consistent capitalised form everywhere."""
    return {"plain": "PLAIN", "lf_fta": "LF-FTA", "lf_fgac": "LF-FGAC"}.get(
        mode, (mode or "").upper().replace("_", "-"))


def release_label(rel: str) -> str:
    """EMR-7.13.0 — always upper case, always prefixed."""
    r = (rel or "").strip()
    if not r:
        return "—"
    r = r.replace("emr-", "EMR-").replace("spark-", "SPARK-")
    return r if r.upper().startswith(("EMR-", "SPARK-")) else f"EMR-{r}".upper()


def normalise_prose(text: str) -> str:
    """Display releases and access modes in the report's capitalised form.

    Only the bracketed mode token is rewritten. Rewriting every occurrence turned
    the variant label "7.13.0 plain Glue" into "7.13.0 PLAIN Glue", which reads
    like a typo -- the mode is a value there, not a label.
    """
    import re as _re
    out = _re.sub(r"\bemr-(\d+(?:\.\d+)*)", lambda mm: f"EMR-{mm.group(1)}", text or "")
    for raw, nice in (("plain", "PLAIN"), ("lf_fgac", "LF-FGAC"), ("lf_fta", "LF-FTA")):
        out = out.replace(f", {raw}]", f", {nice}]")
    return out


def variant_option_text(v: dict) -> str:
    """One standard option format: EMR-7.13.0 · LF-FGAC · X86_64 · id"""
    return (f'{release_label(v.get("release_label"))} · {access_label(v.get("access_mode"))} · '
            f'{(v.get("architecture") or "").upper()} · {v.get("variant_id")}')

CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --ink:#0f1b2a; --ink2:#5f6b7a; --ink3:#8994a3;
  --bg:#f4f6f8; --surface:#fff; --line:#dfe3e8; --line2:#eef1f4;
  --blue:#0972d3; --blue-d:#033160; --blue-bg:#f0f7ff;
  --ok:#037f0c; --ok-bg:#f2fcf3; --crit:#d91515; --crit-bg:#fff5f5;
  --warn:#8d6605; --warn-bg:#fffce9; --info:#5f3dc4; --info-bg:#f6f3ff;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--blue)}
.wrap{max-width:1240px;margin:0 auto;padding:0 24px 72px}

/* header */
header{background:linear-gradient(135deg,#0f1b2a 0%,#16283d 55%,#1c3a57 100%);color:#fff;
  padding:26px 0 22px;border-bottom:3px solid var(--blue)}
header .wrap{padding-bottom:0}
.brand{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.badge{flex:none;border-radius:10px;box-shadow:0 2px 6px rgba(0,0,0,.28)}
.brandtext h1{margin:0;font-size:23px;letter-spacing:-.3px;font-weight:680}
.brandtext .sub{color:#9db4cd;font-size:13px}
.divider{width:1px;height:34px;background:rgba(255,255,255,.22)}

/* sticky section nav */
nav.secnav{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.94);
  backdrop-filter:saturate(180%) blur(8px);border-bottom:1px solid var(--line);
  box-shadow:0 1px 3px rgba(15,27,42,.06)}
nav.secnav .wrap{display:flex;gap:4px;flex-wrap:wrap;padding:0 24px}
nav.secnav a{padding:12px 13px;font-size:12.5px;font-weight:650;color:var(--ink2);
  text-decoration:none;border-bottom:2px solid transparent}
nav.secnav a:hover{color:var(--ink);border-bottom-color:var(--line)}
nav.secnav a.on{color:var(--blue-d);border-bottom-color:var(--blue)}

/* hero strip for the selected pair */
.hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:14px 0 4px}
.hero .cell{background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:14px 15px}
.hero .cell .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.7px;color:var(--ink2);
  font-weight:650}
.hero .cell .v{font-size:24px;font-weight:680;letter-spacing:-.5px;margin-top:5px}
.hero .cell .n{font-size:11.5px;color:var(--ink3);margin-top:2px}
.hero .cell.crit{border-top:3px solid var(--crit)}.hero .cell.ok{border-top:3px solid var(--ok)}
.hero .cell.warn{border-top:3px solid #d9a415}.hero .cell.info{border-top:3px solid var(--info)}

/* pair matrix */
.pm{overflow-x:auto}
.pm table{width:auto;border-collapse:separate;border-spacing:3px;font-size:11px}
.pm th{background:transparent;border:none;position:static;padding:0 8px 5px 0;white-space:nowrap;
  font-size:10.5px;letter-spacing:.4px}
.pm th.rh{text-align:right;font-weight:650;color:var(--ink);padding-right:9px}
.pm td{padding:0;border:none}
.pm i{display:flex;align-items:center;justify-content:center;width:92px;height:27px;border-radius:5px;
  font-size:10px;font-weight:700;letter-spacing:.3px;color:#fff;cursor:help}
.pm i.self{background:#f1f3f5;color:var(--ink3);font-weight:600}
.ribbon{margin-left:auto;background:#8d6605;color:#fff;font-size:11px;font-weight:700;
  letter-spacing:.9px;padding:5px 11px;border-radius:4px;text-transform:uppercase}
.meta{display:flex;gap:26px;flex-wrap:wrap;margin-top:16px;font-size:12.5px;color:#b9cadb}
.meta b{color:#fff;font-weight:600}
.notice{margin-top:16px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.16);
  border-left:3px solid #d9a415;padding:10px 14px;border-radius:0 6px 6px 0;
  font-size:12.5px;color:#d8e4ef;max-width:none}

/* dashboard */
h2.sec{font-size:16px;margin:34px 0 12px;letter-spacing:-.1px;display:flex;align-items:center;gap:9px}
h2.sec::before{content:"";width:3px;height:16px;background:var(--blue);border-radius:2px}
h3.sub{font-size:13px;text-transform:uppercase;letter-spacing:.7px;color:var(--ink2);
  margin:22px 0 9px;font-weight:650}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;
  box-shadow:0 1px 2px rgba(15,27,42,.05)}
.pad{padding:18px}

/* tabs */
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:26px 0 0;border-bottom:1px solid var(--line)}
.tab{appearance:none;background:transparent;border:1px solid transparent;border-bottom:none;
  padding:10px 15px;font:inherit;font-weight:600;font-size:13px;color:var(--ink2);cursor:pointer;
  border-radius:7px 7px 0 0;display:flex;align-items:center;gap:8px;margin-bottom:-1px}
.tab:hover{color:var(--ink);background:#fff}
.tab[aria-selected=true]{background:var(--surface);border-color:var(--line);color:var(--ink);
  box-shadow:0 -2px 0 var(--blue) inset}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
.dot.ok{background:var(--ok)}.dot.warn{background:#d9a415}.dot.crit{background:var(--crit)}
.dot.info{background:var(--info)}
.cmp[hidden]{display:none}

/* verdict */
.verdict{display:flex;gap:16px;align-items:flex-start;border-radius:8px;padding:18px 20px;margin:20px 0;
  border:1px solid var(--line);border-left-width:5px;background:var(--surface)}
.verdict.crit{border-left-color:var(--crit);background:var(--crit-bg)}
.verdict.warn{border-left-color:#d9a415;background:var(--warn-bg)}
.verdict.ok{border-left-color:var(--ok);background:var(--ok-bg)}
.verdict.info{border-left-color:var(--info);background:var(--info-bg)}
.verdict .lvl{font-size:11px;font-weight:800;letter-spacing:1px;text-transform:uppercase;
  padding:5px 10px;border-radius:4px;color:#fff;flex:none;margin-top:1px}
.verdict.crit .lvl{background:var(--crit)}.verdict.warn .lvl{background:#8d6605}
.verdict.ok .lvl{background:var(--ok)}
.verdict.info .lvl{background:var(--info)}
.verdict h3{margin:0 0 7px;font-size:16px}
.verdict ul{margin:0;padding-left:19px}.verdict li{margin:3px 0}

/* kpis */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:12px;margin:16px 0}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:13px 15px}
.kpi .k{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--ink2);font-weight:650}
.kpi .v{font-size:27px;font-weight:680;letter-spacing:-.6px;margin-top:5px;line-height:1.1}
.kpi .n{font-size:11.5px;color:var(--ink3);margin-top:3px}
.kpi.crit .v{color:var(--crit)}.kpi.ok .v{color:var(--ok)}.kpi.warn .v{color:#8d6605}

/* tables */
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--ink2);
  padding:9px 10px;border-bottom:1px solid var(--line);background:#fafbfc;
  position:sticky;top:0;z-index:1;font-weight:650}
td{padding:8px 10px;border-bottom:1px solid var(--line2);vertical-align:top}
tbody tr:hover td{background:#fafcff}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{max-height:520px;overflow:auto;border-radius:8px}
table.dash td:first-child{min-width:210px}
table.dash th{white-space:nowrap}
code,.mono{font-family:var(--mono);font-size:12px}

/* chips */
.chip{display:inline-block;font-size:10.5px;font-weight:700;letter-spacing:.4px;padding:2px 7px;
  border-radius:3px;white-space:nowrap;border:1px solid transparent}
.chip.ok{background:var(--ok-bg);color:var(--ok);border-color:#b3e6b8}
.chip.crit{background:var(--crit-bg);color:var(--crit);border-color:#f5c2c2}
.chip.warn{background:var(--warn-bg);color:var(--warn);border-color:#f0e0a0}
.chip.info{background:var(--info-bg);color:var(--info);border-color:#ded4fb}
.chip.neutral{background:#f2f4f6;color:var(--ink2);border-color:#e2e6ea}
.chip.muted{background:#f7f8f9;color:var(--ink3);border-color:#eceff1}

/* findings */
.finding{border:1px solid var(--line);border-left-width:4px;border-radius:0 8px 8px 0;
  background:var(--surface);padding:15px 17px;margin:11px 0}
.finding.crit{border-left-color:var(--crit)}.finding.warn{border-left-color:#d9a415}
.finding .top{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.finding h4{margin:0;font-size:14.5px}
.ev{margin:11px 0 0;border-collapse:collapse;font-size:12.5px;width:auto;min-width:min(560px,100%)}
.ev td{padding:5px 14px 5px 0;border-bottom:1px dotted var(--line);vertical-align:top}
.ev td:first-child{color:var(--ink2);white-space:nowrap;width:1%}
.note{margin-top:10px;font-size:12.5px;color:var(--ink2);background:#fafbfc;
  border:1px solid var(--line2);border-radius:6px;padding:9px 11px}

/* heatmap */
.hm{overflow-x:auto;padding-bottom:6px}
.hm table{width:auto;font-size:11px;border-collapse:separate;border-spacing:2px}
.hm th{background:transparent;border:none;position:static;padding:0 6px 4px 0;
  font-size:10px;letter-spacing:.3px;white-space:nowrap}
.hm th.rot{height:146px;vertical-align:bottom;padding:0}
.hm th.rot div{transform:translateX(10px) rotate(-58deg);transform-origin:bottom left;
  width:16px;white-space:nowrap;text-align:left}
.hm th.rh{text-align:right;font-weight:650;color:var(--ink);font-size:11px;padding-right:8px;
  text-transform:uppercase;letter-spacing:.4px}
.hm td{padding:0;border:none;width:17px;height:17px}
.hm i{display:block;width:17px;height:17px;border-radius:3px;cursor:help}
.hm i.absent,.legend b.absent{background:repeating-linear-gradient(45deg,#fff,#fff 3px,#eef1f4 3px,#eef1f4 6px);
  border:1px solid #eef1f4}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:12px;font-size:11.5px;color:var(--ink2)}
.legend span{display:flex;align-items:center;gap:5px}
.legend b{width:11px;height:11px;border-radius:3px;display:inline-block}

/* selectors */
.pickerbar{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-end;margin:20px 0 6px;
  background:linear-gradient(180deg,#fff 0%,#fafbfc 100%);
  border:1px solid var(--line);border-radius:10px;padding:16px 18px;
  box-shadow:0 1px 3px rgba(15,27,42,.07)}
.pickerbar .arrow{font-size:20px;color:var(--ink3);padding-bottom:7px;flex:none}
.picker{display:flex;flex-direction:column;gap:5px;min-width:180px}
.picker label{font-size:10.5px;text-transform:uppercase;letter-spacing:.7px;color:var(--ink2);
  font-weight:650}
.picker select{appearance:none;font:inherit;font-size:13px;font-weight:600;color:var(--ink);
  background:#fff url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><path d='M0 0l5 6 5-6z' fill='%235f6b7a'/></svg>") no-repeat right 11px center;
  border:1px solid var(--line);border-radius:6px;padding:8px 32px 8px 11px;cursor:pointer;
  min-width:180px}
.picker select:hover{border-color:var(--blue)}
.picker select:focus{outline:2px solid var(--blue);outline-offset:1px}
.picker .hint{font-size:11.5px;color:var(--ink3);padding-bottom:8px;max-width:340px}
.quick{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 0;width:100%;
  border-top:1px dashed var(--line);padding-top:12px}
.quick span.lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.7px;color:var(--ink2);
  font-weight:650;align-self:center}
.quick button{appearance:none;font:inherit;font-size:12px;font-weight:600;cursor:pointer;
  background:var(--blue-bg);border:1px solid #cfe2f7;border-radius:20px;padding:5px 13px;
  color:var(--blue-d)}
.quick button:hover{background:#e3f0fd;border-color:var(--blue)}
.selstate{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0 0;
  font-size:12.5px;color:var(--ink2)}
.filters{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;margin:0 0 11px}
.rowcount{font-size:11.5px;color:var(--ink3);align-self:flex-end;padding-bottom:8px}
.cmp[hidden]{display:none}
.nosel{background:var(--surface);border:1px dashed var(--line);border-radius:10px;
  padding:34px;text-align:center;color:var(--ink2);margin:18px 0}

details{border:1px solid var(--line);border-radius:8px;background:var(--surface);margin:9px 0}
details summary{cursor:pointer;padding:12px 15px;font-weight:600;font-size:13.5px;
  display:flex;align-items:center;gap:9px}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"▸";color:var(--ink3);font-size:12px}
details[open] summary::before{content:"▾"}
details .body{padding:0 15px 15px}
pre{background:#0f1b2a;color:#dbe6f0;padding:12px 14px;border-radius:6px;overflow-x:auto;
  font-family:var(--mono);font-size:11.5px;line-height:1.5;margin:9px 0}
.err{background:#fffafa;border:1px solid #f5d6d6;border-radius:6px;padding:9px 11px;
  font-family:var(--mono);font-size:11.5px;color:#7a1414;white-space:pre-wrap;word-break:break-word}

/* env cards */
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}
dl.kv{margin:0;display:grid;grid-template-columns:auto 1fr;gap:5px 14px;font-size:12.5px}
dl.kv dt{color:var(--ink2)}dl.kv dd{margin:0;font-family:var(--mono);font-size:12px;word-break:break-all}
.tag{display:inline-block;background:#eef4fb;color:var(--blue-d);border:1px solid #cfe2f7;
  border-radius:4px;padding:1px 7px;font-size:11px;font-weight:600}

footer{border-top:1px solid var(--line);margin-top:44px;padding-top:20px;font-size:12.5px;color:var(--ink2)}
@media print{
  body{background:#fff}.pickerbar,.filters{display:none}.cmp[hidden]{display:block!important}
  .scroll{max-height:none;overflow:visible}header{background:#0f1b2a}
}
"""

JS = """
// Three selections: source release, candidate release, access mode. The mode
// option carries both sides ("PLAIN|LF_FGAC" for a cross-mode comparison), so
// turning governance on is one choice rather than two. A (release, mode) pair
// resolves to a variant id through the embedded map.
(function () {
  var srcRel = document.getElementById('srcRel');
  var dstRel = document.getElementById('dstRel');
  var modeSel = document.getElementById('modeSel');
  var mapEl = document.getElementById('vmap');
  if (!srcRel || !dstRel || !modeSel || !mapEl) { return; }
  var VMAP = JSON.parse(mapEl.textContent);

  function label(sel) { return sel.options[sel.selectedIndex].textContent.trim(); }

  function show() {
    var modes = modeSel.value.split('|');
    var sid = VMAP[srcRel.value + '|' + modes[0]];
    var did = VMAP[dstRel.value + '|' + modes[1]];
    var id = (sid && did) ? ('pair--' + sid + '--' + did) : null;
    var found = false;
    document.querySelectorAll('.cmp').forEach(function (s) {
      var on = (id && s.id === id);
      s.hidden = !on;
      if (on) { found = true; }
    });

    var badge = document.getElementById('selBadge');
    if (badge) {
      var mtxt = label(modeSel).split('\\u2192');
      badge.textContent = label(srcRel) + ' ' + (mtxt[0] || '').trim() + ' \\u2192 '
                        + label(dstRel) + ' ' + ((mtxt[1] || mtxt[0]) || '').trim();
    }

    var none = document.getElementById('noSelection');
    if (!none) { return; }
    none.hidden = found;
    if (found) { return; }
    if (!sid || !did) {
      var missing = [];
      if (!sid) { missing.push(label(srcRel) + ' in this access mode'); }
      if (!did) { missing.push(label(dstRel) + ' in this access mode'); }
      none.textContent = 'This run has no variant for ' + missing.join(' and ')
        + '. Not every release was run in every access mode.';
    } else if (sid === did) {
      none.textContent = 'Source and candidate resolve to the same variant. '
        + 'Change a release or the access mode to compare two different things.';
    } else {
      none.textContent = 'That pair was not precomputed in this report.';
    }
  }

  [srcRel, dstRel, modeSel].forEach(function (s) { s.addEventListener('change', show); });

  document.querySelectorAll('.quick button').forEach(function (b) {
    b.addEventListener('click', function () {
      srcRel.value = b.dataset.srcrel;
      dstRel.value = b.dataset.dstrel;
      modeSel.value = b.dataset.mode;
      show();
      window.scrollTo({ top: document.querySelector('.pickerbar').offsetTop - 12,
                        behavior: 'smooth' });
    });
  });
  show();
})();

// Sticky nav. Section anchors live inside every precomputed pair, so a link
// resolves against the *visible* pair rather than a fixed id -- ids would
// otherwise be duplicated once per pair.
(function () {
  var links = Array.prototype.slice.call(document.querySelectorAll('nav.secnav a'));
  function target(a) {
    var key = a.dataset.goto;
    if (key) {
      var vis = document.querySelector('.cmp:not([hidden])');
      return vis ? vis.querySelector('[data-anchor="' + key + '"]') : null;
    }
    var href = a.getAttribute('href');
    return (href && href.length > 1) ? document.querySelector(href) : null;
  }
  links.forEach(function (a) {
    a.addEventListener('click', function (ev) {
      var el = target(a);
      if (!el) { return; }
      ev.preventDefault();
      window.scrollTo({ top: el.getBoundingClientRect().top + window.pageYOffset - 64,
                        behavior: 'smooth' });
    });
  });
  function sync() {
    var best = null, bestTop = -1e9;
    links.forEach(function (a) {
      var el = target(a);
      if (!el || el.offsetParent === null) { return; }
      var top = el.getBoundingClientRect().top - 90;
      if (top <= 0 && top > bestTop) { bestTop = top; best = a; }
    });
    links.forEach(function (a) { a.classList.toggle('on', a === best); });
  }
  window.addEventListener('scroll', sync, { passive: true });
  sync();
})();

// Table filters. Each <select data-key="verdict"> filters rows in its scope by
// the row's data-<key> attribute; all active selects must match.
document.querySelectorAll('.filters').forEach(function (bar) {
  var scope = document.getElementById(bar.dataset.scope);
  if (!scope) { return; }
  var selects = bar.querySelectorAll('select[data-key]');
  var counter = bar.querySelector('.rowcount');
  function apply() {
    var rows = scope.querySelectorAll('tbody tr');
    var shown = 0;
    rows.forEach(function (tr) {
      var ok = true;
      selects.forEach(function (sel) {
        if (sel.value && tr.dataset[sel.dataset.key] !== sel.value) { ok = false; }
      });
      tr.hidden = !ok;
      if (ok) { shown++; }
    });
    if (counter) { counter.textContent = shown + ' of ' + rows.length + ' shown'; }
  }
  selects.forEach(function (sel) { sel.addEventListener('change', apply); });
  apply();
});
"""


# ------------------------------------------------------------------- helpers

def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def chip(label: str, kind: str | None = None) -> str:
    k = kind or CHIP.get(label, "neutral")
    return f'<span class="chip {k}">{e(label.replace("_", " "))}</span>'


def fmt_pct(v, digits: int = 1, signed: bool = True) -> str:
    if v is None:
        return "—"
    s = f"{v:+.{digits}f}" if signed else f"{v:.{digits}f}"
    return f"{s}%"


def fmt_s(v) -> str:
    return "—" if v is None else f"{v:,.1f}s"


def _pct_or_dash(v) -> str:
    return "—" if v is None else f"{v:.1f}%"


def _s_or_dash(v) -> str:
    return "—" if not v else f"{v:,.0f}s"


def _picker(label: str, key: str, options: list[tuple[str, str]],
            all_label: str = "All") -> str:
    """A labelled <select> that filters rows by their data-<key> attribute."""
    opts = [f'<option value="">{e(all_label)}</option>']
    for value, text in options:
        opts.append(f'<option value="{e(value)}">{e(text)}</option>')
    return (f'<div class="picker"><label>{e(label)}</label>'
            f'<select data-key="{e(key)}">{"".join(opts)}</select></div>')


# --------------------------------------------------------------------- charts

def svg_waterfall(rows: list[dict], noise: float, width: int = 860) -> str:
    rows = [r for r in rows if r["delta_pct"] is not None or r["verdict"] == "NEW_TIMEOUT"]
    if not rows:
        return "<p class='mono'>no comparable queries</p>"
    rh, pad_l, pad_r = 23, 66, 118
    plot = width - pad_l - pad_r
    mid = pad_l + plot / 2
    span = max(20.0, max(abs(r["delta_pct"] or 0) for r in rows) * 1.12)
    h = len(rows) * rh + 42

    def x(pct: float) -> float:
        return mid + (pct / span) * (plot / 2)

    p = [f'<svg viewBox="0 0 {width} {h}" width="100%" height="{h}" role="img" '
         f'aria-label="Per-query performance delta">']
    # noise band
    p.append(f'<rect x="{x(-noise):.1f}" y="26" width="{x(noise)-x(-noise):.1f}" '
             f'height="{len(rows)*rh}" fill="#eef1f4"/>')
    p.append(f'<text x="{mid:.1f}" y="18" text-anchor="middle" font-size="10" fill="#8994a3">'
             f'± {noise:g}% noise band</text>')
    for tick in (-span, -span / 2, 0, span / 2, span):
        p.append(f'<line x1="{x(tick):.1f}" y1="26" x2="{x(tick):.1f}" y2="{26+len(rows)*rh}" '
                 f'stroke="{"#8994a3" if tick==0 else "#e9ecef"}" stroke-width="1"/>')
        p.append(f'<text x="{x(tick):.1f}" y="{26+len(rows)*rh+14}" text-anchor="middle" '
                 f'font-size="9.5" fill="#8994a3">{tick:+.0f}%</text>')

    for i, r in enumerate(rows):
        y = 26 + i * rh
        p.append(f'<text x="{pad_l-9}" y="{y+15}" text-anchor="end" font-size="11.5" '
                 f'fill="#0f1b2a" font-family="ui-monospace,Menlo,monospace">{e(r["name"])}</text>')
        if r["verdict"] == "NEW_TIMEOUT":
            p.append(f'<rect x="{mid:.1f}" y="{y+5}" width="{plot/2:.1f}" height="13" rx="2" '
                     f'fill="#d91515" opacity=".18"/>')
            p.append(f'<text x="{mid+7:.1f}" y="{y+15.5}" font-size="10.5" fill="#d91515" '
                     f'font-weight="700">TIMEOUT</text>')
            continue
        d = r["delta_pct"]
        col = ("#d91515" if r["verdict"] == "REGRESSION"
               else "#5f3dc4" if r["verdict"] == "OVERHEAD"
               else "#037f0c" if r["verdict"] == "IMPROVEMENT" else "#8994a3")
        x0, x1 = (mid, x(d)) if d >= 0 else (x(d), mid)
        p.append(f'<rect x="{x0:.1f}" y="{y+4.5}" width="{max(1.2,x1-x0):.1f}" height="14" rx="2" fill="{col}"/>')
        lx = (x1 + 7) if d >= 0 else (x0 - 7)
        anc = "start" if d >= 0 else "end"
        p.append(f'<text x="{lx:.1f}" y="{y+15.5}" text-anchor="{anc}" font-size="11" fill="{col}" '
                 f'font-weight="650">{d:+.1f}%</text>')
        p.append(f'<text x="{width-6}" y="{y+15.5}" text-anchor="end" font-size="10.5" fill="#8994a3" '
                 f'font-family="ui-monospace,Menlo,monospace">'
                 f'{r["base_best_s"]:.1f}→{r["cand_best_s"]:.1f}s</text>')
    p.append("</svg>")
    return "".join(p)


def svg_hbars(items: list[tuple[str, float, str]], unit: str = "", width: int = 860,
              value_fmt: str = "{:,.2f}") -> str:
    """items: (label, value, colour)."""
    if not items:
        return ""
    rh, pad_l, pad_r = 30, 200, 110
    plot = width - pad_l - pad_r
    vmax = max(v for _, v, _ in items) or 1.0
    h = len(items) * rh + 12
    p = [f'<svg viewBox="0 0 {width} {h}" width="100%" height="{h}" role="img" aria-label="comparison bars">']
    for i, (label, val, col) in enumerate(items):
        y = i * rh + 6
        w = max(2.0, val / vmax * plot)
        p.append(f'<text x="{pad_l-10}" y="{y+15}" text-anchor="end" font-size="11.5" fill="#0f1b2a">{e(label)}</text>')
        p.append(f'<rect x="{pad_l}" y="{y+3}" width="{w:.1f}" height="17" rx="3" fill="{col}"/>')
        p.append(f'<text x="{pad_l+w+8:.1f}" y="{y+16}" font-size="11.5" fill="#5f6b7a" '
                 f'font-variant-numeric="tabular-nums">{value_fmt.format(val)}{e(unit)}</text>')
    p.append("</svg>")
    return "".join(p)


# Heatmap columns read left-to-right in table-lifecycle order rather than
# alphabetically: metadata, create, write, mutate, alter, drop, writers,
# then the Lake Formation data-filter reads.
OP_ORDER = [
    "DESCRIBE", "SHOW_CREATE_TABLE", "SHOW_PARTITIONS", "SHOW_TBLPROPERTIES", "SHOW_COLUMNS",
    "METADATA_TABLES",
    "CREATE_TABLE", "CREATE_TABLE_LIKE", "CTAS", "REPLACE_TABLE_AS_SELECT",
    "SELECT", "TIME_TRAVEL",
    "INSERT_INTO", "INSERT_OVERWRITE", "LOAD_DATA",
    "UPDATE", "DELETE", "MERGE_INTO", "TRUNCATE_TABLE",
    "ALTER_TABLE", "ALTER_TABLE_SET_LOCATION", "ALTER_TABLE_RENAME", "ADD_PARTITION", "REPAIR_TABLE",
    "DROP_TABLE",
    "DF_WRITER_V1", "DF_WRITER_V2", "STORED_PROCEDURES", "TABLE_MAINTENANCE",
    "ROW_FILTER", "COLUMN_FILTER", "CELL_FILTER", "NESTED_FILTER",
]


def heatmap(rows: list[dict]) -> str:
    formats, ops = [], []
    cells: dict[tuple[str, str], dict] = {}
    for r in rows:
        f, o = r["table_format"], r["operation"]
        if f not in formats:
            formats.append(f)
        if o not in ops:
            ops.append(o)
        cells[(f, o)] = r
    formats.sort()
    ops.sort(key=lambda o: (OP_ORDER.index(o) if o in OP_ORDER else len(OP_ORDER), o))
    out = ['<div class="hm"><table><thead><tr><th></th>']
    for o in ops:
        out.append(f'<th class="rot"><div>{e(o)}</div></th>')
    out.append("</tr></thead><tbody>")
    for f in formats:
        out.append(f'<tr><th class="rh">{e(f)}</th>')
        for o in ops:
            r = cells.get((f, o))
            if not r:
                out.append('<td><i class="absent" title="not exercised for this format"></i></td>')
                continue
            col = HEAT.get(r["verdict"], "#dfe3e8")
            tip = (f'{f}.{o} — {r["verdict"].replace("_", " ")}\n'
                   f'baseline: {r["base_status"]} (expected {STATE_LABEL.get(r["expected_base"], r["expected_base"])})\n'
                   f'candidate: {r["cand_status"]} (expected {STATE_LABEL.get(r["expected_cand"], r["expected_cand"])})')
            out.append(f'<td><i style="background:{col}" title="{e(tip)}"></i></td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    seen = {r["verdict"] for r in rows}
    out.append('<div class="legend">')
    for v, col in HEAT.items():
        if v in seen:
            out.append(f'<span><b style="background:{col}"></b>{e(v.replace("_", " ").title())}</span>')
    out.append('<span><b class="absent"></b>Not exercised for this format</span>')
    out.append("</div>")
    return "".join(out)


# -------------------------------------------------------------------- sections

def hero(res: dict) -> str:
    """Headline numbers for the selected pair, above the detail sections."""
    fn = res["functional"] or {"counts": {}}
    c = fn["counts"]
    perf = res["performance"] or {}
    agg = perf.get("aggregate") or {}
    gov = res["comparison"].get("intent") == "governance_overhead"
    new_corr = len([f for f in res["correctness"]
                    if not f["pre_existing"] and not f.get("resolved")])
    tb = sum(x["base_usd"] for x in res["cost"]) or 0.0
    tc = sum(x["cand_usd"] for x in res["cost"]) or 0.0
    cost_delta = ((tc - tb) / tb * 100) if tb else None

    cells = [
        ("New failures", c.get("NEW_FAILURE", 0), "crit" if c.get("NEW_FAILURE") else "", ""),
        ("Fixed", c.get("FIXED", 0) + c.get("FIXED_BY_RELEASE", 0),
         "ok" if (c.get("FIXED", 0) + c.get("FIXED_BY_RELEASE", 0)) else "", ""),
        ("Correctness", new_corr, "crit" if new_corr else "", "new findings"),
        ("Query time", fmt_pct(agg.get("geomean_delta_pct")),
         ("info" if gov else ("crit" if (agg.get("geomean_delta_pct") or 0) > 0 else "ok"))
         if agg.get("geomean_delta_pct") is not None else "",
         "geomean" + (" · overhead" if gov else "")),
        ("Cost", fmt_pct(cost_delta),
         "info" if gov else ("crit" if (cost_delta or 0) > 0 else "ok"),
         f"${tb:,.3f} → ${tc:,.3f}"),
        ("Expected unsupported", c.get("EXPECTED_UNSUPPORTED", 0), "", "not regressions"),
    ]
    out = ['<div class="hero">']
    for k, v, kind, note in cells:
        out.append(f'<div class="cell {kind}"><div class="k">{e(k)}</div>'
                   f'<div class="v">{e(v)}</div>'
                   + (f'<div class="n">{e(note)}</div>' if note else "")
                   + "</div>")
    out.append("</div>")
    return "".join(out)


def pair_matrix(results: list[dict], variants: dict) -> str:
    """Verdict for every source → destination pair, at a glance."""
    ids = list(variants.keys())
    by_pair = {(r["comparison"]["baseline"], r["comparison"]["candidate"]): r for r in results}
    short = {vid: f'{release_label(v["release_label"]).replace("EMR-", "")}·{access_label(v["access_mode"])}'
             for vid, v in variants.items()}
    colour = {"BLOCK": "#d91515", "CAUTION": "#d9a415", "PROCEED": "#037f0c",
              "INDETERMINATE": "#5f3dc4"}
    short_verdict = {"BLOCK": "BLOCK", "CAUTION": "CAUTION", "PROCEED": "PASS",
                     "INDETERMINATE": "NO VERDICT"}
    out = ['<div class="pm"><table><thead><tr><th></th>']
    for d in ids:
        out.append(f'<th>{e(short[d])}</th>')
    out.append("</tr></thead><tbody>")
    for s in ids:
        out.append(f'<tr><th class="rh">{e(short[s])}</th>')
        for d in ids:
            if s == d:
                out.append('<td><i class="self">—</i></td>')
                continue
            r = by_pair.get((s, d))
            if not r:
                out.append('<td><i class="self">n/a</i></td>')
                continue
            lvl = r["verdict"]["level"]
            tip = (f'{variants[s].get("label", s)} → {variants[d].get("label", d)}\n'
                   f'{lvl} · {r["comparison"]["intent"].replace("_", " ")} · '
                   f'{r["match"]["status"]}')
            out.append(f'<td><i style="background:{colour.get(lvl, "#8994a3")}" '
                       f'title="{e(tip)}">{e(short_verdict.get(lvl, lvl))}</i></td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    out.append('<p style="margin:10px 0 0;font-size:12px;color:var(--ink3)">'
               'Rows are the source (baseline), columns the destination (candidate). '
               'Hover a cell for the intent and match status.</p>')
    return "".join(out)


def sec_correctness(findings: list[dict]) -> str:
    out = ['<h2 class="sec">Correctness</h2>']
    if not findings:
        out.append('<div class="card pad">No correctness findings. Row counts, result-set checksums, '
                   'commit-log advancement and post-operation object listings all agreed.</div>')
        return "".join(out)
    out.append('<p style="margin:0 0 12px;color:var(--ink2)">Checked ahead of performance: a job that '
               'exits 0 with the wrong data is worse than a slow one.</p>')
    for f in findings:
        cls = ("ok" if f.get("resolved") else
               "crit" if f["severity"] == "critical" else "warn")
        out.append(f'<div class="finding {cls}"><div class="top">{chip(f["category"])}'
                   f'{chip(f["severity"].upper(), cls)}'
                   f'<h4>{e(f["unit_label"])}</h4>'
                   f'<span class="tag">{e(f.get("table_type") or "")}</span>')
        if f["pre_existing"]:
            out.append(chip("PRE-EXISTING", "muted"))
        if f.get("resolved"):
            out.append(chip("RESOLVED", "ok"))
        out.append("</div>")
        out.append(f'<p style="margin:9px 0 0">{e(f["summary"])}</p><table class="ev">')
        for k, v in f["evidence"]:
            out.append(f"<tr><td>{e(k)}</td><td class='mono'>{e(v)}</td></tr>")
        out.append("</table>")
        if f.get("note"):
            out.append(f'<div class="note">{e(f["note"])}</div>')
        if f.get("job_id"):
            out.append(f'<div class="note" style="background:#fff">Repro: '
                       f'<code>etd run --spec repro/{e(f["unit_label"].replace(".", "-"))}.yaml</code> · '
                       f'job <code>{e(f["job_id"])}</code></div>')
        out.append("</div>")
    return "".join(out)


def sec_functional(fn: dict, scope_id: str) -> str:
    if not fn:
        return ""
    c = fn["counts"]
    out = ['<h2 class="sec">Functional</h2>']
    out.append('<div class="kpis">')
    for key, label, kind in [
        ("NEW_FAILURE", "New failures", "crit"),
        ("STABLE_FAIL", "Failing on both", "warn"),
        ("FLAKY", "Flaky", "warn"),
        ("FIXED_BY_RELEASE", "Fixed by release", "ok"),
        ("STABLE_PASS", "Stable pass", ""),
        ("EXPECTED_UNSUPPORTED", "Expected unsupported", ""),
    ]:
        n = c.get(key, 0)
        out.append(f'<div class="kpi {kind if n else ""}"><div class="k">{e(label)}</div>'
                   f'<div class="v">{n}</div></div>')
    out.append("</div>")

    out.append('<h3 class="sub">Operation matrix — format × operation</h3>')
    out.append('<div class="card pad">' + heatmap(fn["rows"]) + "</div>")

    if fn["clusters"]:
        out.append('<h3 class="sub">Error clusters — '
                   f'{len(fn["clusters"])} root cause(s) behind '
                   f'{sum(cl["count"] for cl in fn["clusters"])} failure(s)</h3>')
        for cl in fn["clusters"]:
            out.append(f'<details><summary>{chip("×" + str(cl["count"]), "crit")}'
                       f'<code>{e(cl["cluster_id"])}</code>'
                       f'<span style="font-weight:500;color:var(--ink2)">{e(cl["signature"][:110])}…</span>'
                       f'</summary><div class="body">'
                       f'<div class="err">{e(cl["representative"])}</div>'
                       f'<p style="margin:9px 0 0;font-size:12.5px;color:var(--ink2)">Affected: '
                       f'<code>{e(", ".join(cl["members"]))}</code></p></div></details>')

    order = ["NEW_FAILURE", "STABLE_FAIL", "FLAKY", "EXPECTED_REMOVED", "FIXED_BY_RELEASE",
             "FIXED", "EXPECTED_UNSUPPORTED", "STABLE_PASS", "NOT_COMPARABLE", "MISSING"]
    rows = sorted(fn["rows"], key=lambda r: (order.index(r["verdict"]) if r["verdict"] in order else 99,
                                             r["unit_label"]))
    out.append('<h3 class="sub">All operations</h3>')
    verdicts = [v for v in order if c.get(v)]
    formats = sorted({r["table_format"] for r in rows})
    ttypes = sorted({r.get("table_type") or "" for r in rows} - {""})
    out.append(f'<div class="filters" data-scope="{scope_id}">')
    out.append(_picker("Verdict", "verdict",
                       [(v, f'{v.replace("_", " ").title()} ({c[v]})') for v in verdicts]))
    out.append(_picker("Table format", "format", [(f, f) for f in formats]))
    if ttypes:
        out.append(_picker("Table type", "tabletype", [(t, t) for t in ttypes]))
    out.append('<span class="rowcount"></span></div>')
    out.append(f'<div class="card scroll" id="{scope_id}"><table><thead><tr>'
               '<th>Operation</th><th>Format</th><th>Table type</th><th>Verdict</th>'
               '<th>Baseline</th><th>Candidate</th><th>Expected</th>'
               '<th class="num">Base</th><th class="num">Cand</th><th>LF permissions</th>'
               "</tr></thead><tbody>")
    for r in rows:
        exp = STATE_LABEL.get(r["expected_cand"], r["expected_cand"])
        if r["expected_base"] != r["expected_cand"]:
            exp = f'{STATE_LABEL.get(r["expected_base"], r["expected_base"])} → {exp}'
        out.append(
            f'<tr data-verdict="{e(r["verdict"])}" data-format="{e(r["table_format"])}" '
            f'data-tabletype="{e(r.get("table_type") or "")}">'
            f'<td class="mono">{e(r["operation"])}</td><td>{e(r["table_format"])}</td>'
            f'<td style="color:var(--ink2)">{e(r.get("table_type") or "")}</td>'
            f'<td>{chip(r["verdict"])}</td>'
            f'<td>{e(r["base_status"])}</td><td>{e(r["cand_status"])}</td>'
            f'<td style="font-size:12px;color:var(--ink2)">{e(exp)}</td>'
            f'<td class="num">{fmt_s(r.get("base_duration_s"))}</td>'
            f'<td class="num">{fmt_s(r.get("cand_duration_s"))}</td>'
            f'<td class="mono" style="color:var(--ink2)">{e(", ".join(r.get("lf_permissions") or []))}</td></tr>')
    out.append("</tbody></table></div>")
    return "".join(out)


def sec_perf(perf: dict, match: dict, intent: str = "upgrade_regression",
             scope_suffix: str = "0", heading: str = "Performance") -> str:
    if not perf:
        return ""
    governance = intent == "governance_overhead"
    a = perf["aggregate"]
    out = [f'<h2 class="sec">{e(heading)}</h2>']
    if governance:
        out.append('<div class="verdict info"><span class="lvl">overhead, not regression</span><div>'
                   '<h3>This pair measures the price of enabling governance</h3>'
                   '<p style="margin:0">A slowdown here is the expected cost of enforcing access '
                   'control, so it is labelled OVERHEAD and excluded from the pass/fail verdict. '
                   'Reporting it as a regression would tell you to stop enforcing access control.</p>'
                   "</div></div>")
    elif not match["perf_verdict_valid"]:
        out.append(f'<div class="verdict info">'
                   f'<span class="lvl">measurement only</span><div>'
                   f'<h3>Performance verdict suppressed</h3>'
                   f'<p style="margin:0">{e(match["why"])}</p></div></div>')
    out.append('<div class="kpis">')
    tot = a["total_delta_pct"]
    out.append(f'<div class="kpi {"crit" if (tot or 0)>a["noise_band_pct"] else "ok" if (tot or 0)<-a["noise_band_pct"] else ""}">'
               f'<div class="k">Total time</div><div class="v">{fmt_pct(tot)}</div>'
               f'<div class="n">{a["total_base_s"]:,.0f}s → {a["total_cand_s"]:,.0f}s</div></div>')
    g = a["geomean_delta_pct"]
    out.append(f'<div class="kpi {"crit" if (g or 0)>a["noise_band_pct"] else "ok" if (g or 0)<-a["noise_band_pct"] else ""}">'
               f'<div class="k">Geomean per query</div><div class="v">{fmt_pct(g)}</div>'
               f'<div class="n">ratio {a["geomean_ratio"]}</div></div>')
    if governance:
        out.append(f'<div class="kpi"><div class="k">Slower queries</div>'
                   f'<div class="v">{perf["counts"].get("OVERHEAD", 0)}</div>'
                   f'<div class="n">overhead beyond ±{a["noise_band_pct"]:g}% band</div></div>')
    else:
        out.append(f'<div class="kpi crit"><div class="k">Regressions</div>'
                   f'<div class="v">{perf["counts"].get("REGRESSION", 0)}</div>'
                   f'<div class="n">beyond ±{a["noise_band_pct"]:g}% band</div></div>')
    out.append(f'<div class="kpi ok"><div class="k">Improvements</div>'
               f'<div class="v">{perf["counts"].get("IMPROVEMENT", 0)}</div></div>')
    nt = perf["counts"].get("NEW_TIMEOUT", 0)
    out.append(f'<div class="kpi {"crit" if nt else ""}"><div class="k">New timeouts</div>'
               f'<div class="v">{nt}</div></div>')
    out.append(f'<div class="kpi"><div class="k">p95 ratio</div><div class="v">{a["p95_ratio"] or "—"}</div>'
               f'<div class="n">p50 {a["p50_ratio"]}</div></div>')
    out.append("</div>")
    out.append(f'<p style="margin:0 0 4px;color:var(--ink2);font-size:12.5px">Best-of-N per query. '
               f'Run-to-run spread within a variant: baseline up to {a["max_base_spread_pct"]}%, '
               f'candidate up to {a["max_cand_spread_pct"]}% — the ±{a["noise_band_pct"]:g}% band is shaded.</p>')
    worst_spread = max(a["max_base_spread_pct"] or 0, a["max_cand_spread_pct"] or 0)
    if worst_spread > a["noise_band_pct"]:
        out.append(
            f'<div class="note" style="border-color:#f0e0a0;background:var(--warn-bg);margin:0 0 12px">'
            f'<b>Noise band is tighter than observed noise.</b> At least one query varied by '
            f'{worst_spread:.1f}% between iterations <em>on the same variant</em>, which is wider than the '
            f'±{a["noise_band_pct"]:g}% band used to classify verdicts. Deltas smaller than '
            f'{worst_spread:.1f}% should be treated as unresolved rather than real. Either raise '
            f'<code>perf_noise_band_pct</code> to {worst_spread:.0f}, or add iterations to tighten the '
            f'estimate.</div>')
    out.append('<div class="card pad">' + svg_waterfall(perf["rows"], a["noise_band_pct"]) + "</div>")

    out.append('<h3 class="sub">Per-query detail</h3>')
    pv: dict[str, int] = {}
    for r in perf["rows"]:
        pv[r["verdict"]] = pv.get(r["verdict"], 0) + 1
    scope = f"ptab-{scope_suffix}"
    out.append(f'<div class="filters" data-scope="{scope}">')
    out.append(_picker("Verdict", "verdict",
                       [(v, f'{v.replace("_", " ").title()} ({n})')
                        for v, n in sorted(pv.items(), key=lambda kv: -kv[1])]))
    out.append('<span class="rowcount"></span></div>')
    out.append(f'<div class="card scroll" id="{scope}"><table><thead><tr><th>Query</th>'
               '<th>Verdict</th>'
               '<th class="num">Baseline best</th><th class="num">Candidate best</th>'
               '<th class="num">Δ</th><th class="num">Band</th>'
               '<th class="num">Base spread</th><th class="num">Cand spread</th>'
               '<th>Iterations (candidate)</th></tr></thead><tbody>')
    for r in perf["rows"]:
        out.append(
            f'<tr data-verdict="{e(r["verdict"])}"><td class="mono">{e(r["name"])}</td>'
            f'<td>{chip(r["verdict"])}</td>'
            f'<td class="num">{fmt_s(r["base_best_s"])}</td>'
            f'<td class="num">{fmt_s(r["cand_best_s"])}</td>'
            f'<td class="num" style="font-weight:650;color:'
            f'{"#d91515" if (r["delta_pct"] or 0)>a["noise_band_pct"] else "#037f0c" if (r["delta_pct"] or 0)<-a["noise_band_pct"] else "#5f6b7a"}">'
            f'{fmt_pct(r["delta_pct"])}</td>'
            f'<td class="num" style="color:var(--ink3)">'
            f'{fmt_pct(r.get("effective_band_pct"), 1, False)}</td>'
            f'<td class="num" style="color:var(--ink3)">{fmt_pct(r["base_spread_pct"], 1, False)}</td>'
            f'<td class="num" style="color:var(--ink3)">{fmt_pct(r["cand_spread_pct"], 1, False)}</td>'
            f'<td class="mono" style="color:var(--ink2)">'
            f'{e(", ".join(f"{x:.1f}" for x in r["cand_iterations"]) or (r["error"] or ""))}</td></tr>')
    out.append("</tbody></table></div>")
    return "".join(out)


def sec_cost(cost: list[dict], bv: dict, cv: dict, pricing: dict) -> str:
    if not cost:
        return ""
    out = ['<h2 class="sec">Cost</h2>']
    tb = sum(c["base_usd"] for c in cost)
    tc = sum(c["cand_usd"] for c in cost)
    d = (tc - tb) / tb * 100 if tb else None
    out.append('<div class="kpis">')
    out.append(f'<div class="kpi"><div class="k">Baseline · full run</div><div class="v">${tb:,.2f}</div>'
               f'<div class="n">{e(bv["label"])}</div></div>')
    out.append(f'<div class="kpi {"crit" if (d or 0)>0 else "ok"}"><div class="k">Candidate · full run</div>'
               f'<div class="v">${tc:,.2f}</div><div class="n">{fmt_pct(d)} vs baseline</div></div>')
    out.append(f'<div class="kpi"><div class="k">Drivers per job</div>'
               f'<div class="v">{cost[0]["base_drivers"]} → {cost[0]["cand_drivers"]}</div>'
               f'<div class="n">FGAC runs a user + system driver</div></div>')
    out.append("</div>")
    items = []
    for c in cost:
        items.append((f'{c["workload_id"]} · baseline', c["base_usd"], "#8994a3"))
        items.append((f'{c["workload_id"]} · candidate', c["cand_usd"],
                      "#d91515" if (c["delta_pct"] or 0) > 0 else "#037f0c"))
    out.append('<div class="card pad">' + svg_hbars(items, unit=" USD", value_fmt="${:,.3f}") + "</div>")
    out.append('<div class="card scroll" style="margin-top:14px"><table><thead><tr><th>Workload</th>'
               '<th class="num">Base wall</th><th class="num">Cand wall</th>'
               '<th class="num">Base vCPU-hr</th><th class="num">Cand vCPU-hr</th>'
               '<th class="num">Base GB-hr</th><th class="num">Cand GB-hr</th>'
               '<th class="num">Base $</th><th class="num">Cand $</th><th class="num">Δ$</th>'
               "</tr></thead><tbody>")
    for c in cost:
        out.append(f'<tr><td class="mono">{e(c["workload_id"])}</td>'
                   f'<td class="num">{c["base_wall_s"]:,.0f}s</td><td class="num">{c["cand_wall_s"]:,.0f}s</td>'
                   f'<td class="num">{c["base_vcpu_hour"]:,.2f}</td><td class="num">{c["cand_vcpu_hour"]:,.2f}</td>'
                   f'<td class="num">{c["base_gb_hour"]:,.1f}</td><td class="num">{c["cand_gb_hour"]:,.1f}</td>'
                   f'<td class="num">${c["base_usd"]:,.3f}</td><td class="num">${c["cand_usd"]:,.3f}</td>'
                   f'<td class="num" style="font-weight:650">{fmt_pct(c["delta_pct"])}</td></tr>')
    out.append("</tbody></table></div>")
    out.append(f'<p style="margin:11px 0 0;font-size:12px;color:var(--ink3)">Prices as of '
               f'{e(pricing["as_of"])} — {e(pricing["source"])}. {e(pricing["note"])}</p>')
    return "".join(out)


def variant_card(v: dict, role: str) -> str:
    out = [f'<div class="card pad"><div style="display:flex;gap:9px;align-items:center;margin-bottom:11px">'
           f'<span class="tag">{e(role)}</span><b>{e(v["label"])}</b></div><dl class="kv">']
    out.append(f"<dt>release</dt><dd>{e(release_label(v.get('release_label')))}</dd>")
    out.append(f"<dt>access mode</dt><dd>{e(access_label(v.get('access_mode')))}</dd>")
    for k in ["variant_id", "deployment_model", "architecture",
              "shape_hash", "config_hash", "patch_hash"]:
        out.append(f"<dt>{e(k.replace('_', ' '))}</dt><dd>{e(v.get(k) or '—')}</dd>")
    out.append(f'<dt>shape</dt><dd>{e(json.dumps(v["shape"]))}</dd>')
    out.append(f'<dt>env</dt><dd>{e(json.dumps(v["env_handle"]))}</dd>')
    out.append("</dl>")
    if v.get("patch"):
        out.append(f'<div class="note"><b>Patch:</b> {e(v["patch"]["description"])}<br>'
                   f'<code>{e(v["patch"].get("image", {}).get("uri", ""))}</code></div>')
    if v.get("notes"):
        out.append(f'<div class="note">{e(v["notes"])}</div>')
    out.append("</div>")
    return "".join(out)


def sec_env(res: dict) -> str:
    m = res["match"]
    out = ['<h2 class="sec">Environments</h2>']
    out.append(f'<div class="card pad" style="margin-bottom:14px">'
               f'<div style="display:flex;gap:9px;align-items:center;flex-wrap:wrap">'
               f'{chip(m["status"])}<b>{e(m["why"])}</b></div>')
    if m["advisory"]:
        out.append('<div class="note">' + "<br>".join(e(a) for a in m["advisory"]) + "</div>")
    out.append("</div>")
    out.append('<div class="grid2">' + variant_card(res["baseline"], "baseline")
               + variant_card(res["candidate"], "candidate") + "</div>")
    return "".join(out)


def num(value, spec: str = ",.0f", suffix: str = "", prefix: str = "") -> str:
    """Format a number, or an em dash when it is absent.

    A variant can legitimately lack a figure -- no performance workload ran, or no
    expected-support matrix covers its access mode -- and a dashboard that raises
    TypeError on None turns a partial result into no report at all.
    """
    if value is None:
        return "—"
    return f"{prefix}{format(value, spec)}{suffix}"


def dashboard(run, results: list[dict]) -> str:
    """Cross-variant dashboard: one row per variant, independent of any pair."""
    pricing = run.manifest["pricing"]
    perf_wid = next((w for w in run.workloads if run.workloads[w]["kind"] == "performance"), None)
    func_wid = next((w for w in run.workloads if run.workloads[w]["kind"] == "functional"), None)
    perf_label = (perf_wid or "perf").split("/")[-1]

    rows = []
    for v in run.manifest["variants"]:
        vid = v["variant_id"]
        pp, fp = run.payload(vid, perf_wid), run.payload(vid, func_wid)
        best = sum(min(u["iterations"]) for u in pp["units"] if u.get("iterations")) if pp else None
        timeouts = sum(1 for u in pp["units"] if u["status"] == "TIMEOUT") if pp else 0
        total_usd = sum(usd(run.payload(vid, w)["cost_facts"], pricing)
                        for w in run.workloads if run.payload(vid, w))
        units = fp["units"] if fp else []
        expected_ok = [u for u in units if u["expected_state"] in ("S", "S3")]
        passed = [u for u in expected_ok if u["status"] == "SUCCESS"]
        rows.append({
            "v": v, "perf_total_s": best, "timeouts": timeouts, "usd": total_usd,
            "pass_rate": (len(passed) / len(expected_ok) * 100) if expected_ok else None,
            "passed": len(passed), "expected_ok": len(expected_ok),
            "unsupported": sum(1 for u in units if u["expected_state"] == "N"),
        })

    baseline = next(r for r in rows if r["v"].get("baseline"))
    out = ['<p style="margin:0 0 12px;color:var(--ink2)">Every variant in this run, independent of any '
           'single pairing. Use it to see the shape of the whole grid before drilling into a comparison. '
           'Pass rate counts only operations the documentation says should work on that release, so the '
           'denominator moves with the release.</p>',
           '<div class="card scroll"><table class="dash"><thead><tr><th>Variant</th><th>Release</th>'
           '<th>Access</th><th class="num">Pass rate</th>'
           '<th class="num">Doc.&nbsp;unsup.</th><th class="num">Perf total</th>'
           '<th class="num">Timeouts</th><th class="num">Cost&nbsp;/&nbsp;run</th>'
           '<th class="num">vs&nbsp;base</th></tr></thead><tbody>']
    out[-1] = out[-1].replace(">Perf total<", f">{e(perf_label)} total<")
    for r in rows:
        v = r["v"]
        rel = (r["perf_total_s"] / baseline["perf_total_s"] - 1) * 100 if (
            r["perf_total_s"] and baseline["perf_total_s"]) else None
        star = ' <span class="tag">baseline</span>' if v.get("baseline") else ""
        out.append(
            f'<tr><td><b>{e(v["label"])}</b>{star}<br>'
            f'<code style="color:var(--ink3);white-space:nowrap">{e(v["variant_id"])}</code></td>'
            f'<td class="mono" style="white-space:nowrap">{e(release_label(v["release_label"]))}</td>'
            f'<td>{chip(access_label(v["access_mode"]), "info" if v["access_mode"]=="lf_fgac" else "neutral")}</td>'
            f'<td class="num">{num(r["pass_rate"], ".1f", "%")}<br><span style="color:var(--ink3);font-size:11.5px">'
            f'{r["passed"]}/{r["expected_ok"]}</span></td>'
            f'<td class="num" style="color:var(--ink3)">{r["unsupported"]}</td>'
            f'<td class="num">{num(r["perf_total_s"], ",.0f", "s")}</td>'
            f'<td class="num" style="color:{"#d91515" if r["timeouts"] else "var(--ink3)"}">{r["timeouts"]}</td>'
            f'<td class="num">{num(r["usd"], ",.2f", "", "$")}</td>'
            f'<td class="num" style="font-weight:650">{fmt_pct(rel)}</td></tr>')
    out.append("</tbody></table></div>")

    items = [(r["v"]["label"], r["perf_total_s"] or 0,
              "#0972d3" if r["v"].get("baseline") else "#5f3dc4" if r["v"]["access_mode"] == "lf_fgac"
              else "#8994a3") for r in rows]
    out.append('<div class="grid2" style="margin-top:14px">')
    out.append('<div class="card pad"><h3 class="sub" style="margin-top:0">'
               f'{e(perf_label)} total, best-of-N</h3>'
               + svg_hbars(items, unit="s", width=560, value_fmt="{:,.0f}") + "</div>")
    items2 = [(r["v"]["label"], r["usd"],
               "#0972d3" if r["v"].get("baseline") else "#5f3dc4" if r["v"]["access_mode"] == "lf_fgac"
               else "#8994a3") for r in rows]
    out.append('<div class="card pad"><h3 class="sub" style="margin-top:0">Cost per full run</h3>'
               + svg_hbars(items2, unit="", width=560, value_fmt="${:,.2f}") + "</div>")
    out.append("</div>")
    return "".join(out)


# ---------------------------------------------------------------------- render

def render_html(run, results: list[dict]) -> str:
    m = run.manifest
    parts: list[str] = []
    parts.append('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width,initial-scale=1">'
                 f'<title>EMR Test Drive — {e(m["run_id"])}</title><style>{CSS}</style></head><body>')

    # header
    parts.append('<header><div class="wrap"><div class="brand">' + PRODUCT_BADGE
                 + '<div class="divider"></div><div class="brandtext">'
                   '<h1>EMR Test Drive</h1>'
                   '<span class="sub">upgrade, patch and access-mode regression report</span></div>')
    if m.get("data_class") == "SAMPLE":
        parts.append('<span class="ribbon">Sample data</span>')
    parts.append("</div><div class=meta>"
                 f'<span>run <b>{e(m["run_id"])}</b></span>'
                 f'<span>region <b>{e(m["region"])}</b></span>'
                 f'<span>account <b>{e(m["account"])}</b></span>'
                 f'<span>releases <b>'
                 f'{e(" / ".join(sorted({release_label(v["release_label"]) for v in m["variants"]})))}'
                 "</b></span>"
                 f'<span>modes <b>'
                 f'{e(" / ".join(sorted({access_label(v["access_mode"]) for v in m["variants"]})))}'
                 "</b></span>"
                 f'<span>generated <b>{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</b></span>'
                 "</div>")
    parts.append(f'<div class="notice"><b>Scenario.</b> {e(normalise_prose(m["scenario"]))}</div>')
    if m.get("data_class_note"):
        parts.append(f'<div class="notice"><b>Data provenance.</b> {e(m["data_class_note"])}</div>')
    parts.append("</div></header>")

    # sticky section nav
    parts.append('<nav class="secnav"><div class="wrap">'
                 '<a class="on" href="#compare">Compare</a>'
                 '<a href="#" data-goto="correctness">Correctness</a>'
                 '<a href="#" data-goto="functional">Functional</a>'
                 '<a href="#" data-goto="performance">Performance</a>'
                 '<a href="#" data-goto="cost">Cost</a>'
                 '<a href="#variants">Variants</a>'
                 '<a href="#method">Method</a>'
                 "</div></nav><div class=wrap>")

    # ---- Picker: three short dropdowns. Release on each side, access mode across.
    variants = {v["variant_id"]: v for v in m["variants"]}
    modes = sorted({v["access_mode"] for v in m["variants"]})

    # A "build" is what the release dropdown offers. Usually that is just the
    # release label, but two variants can share a release and access mode while
    # differing by patch or Spark config -- keying only on release would silently
    # drop one. When that happens the distinguishing bit is folded into the option
    # text rather than adding a fourth dropdown.
    def build_suffix(v: dict) -> str:
        return "+patch" if v.get("patch_hash") else ""

    collide: dict[tuple, list] = {}
    for v in m["variants"]:
        collide.setdefault((v["release_label"], build_suffix(v), v["access_mode"]), []).append(v)

    def build_key(v: dict) -> str:
        base = f'{v["release_label"]}{build_suffix(v)}'
        group = collide[(v["release_label"], build_suffix(v), v["access_mode"])]
        return f'{base}#{v["variant_id"]}' if len(group) > 1 else base

    def build_text(v: dict) -> str:
        txt = release_label(v["release_label"])
        if build_suffix(v):
            txt += " + patch"
        group = collide[(v["release_label"], build_suffix(v), v["access_mode"])]
        if len(group) > 1:
            txt += f' ({v["variant_id"]})'
        return txt

    builds: dict[str, str] = {}
    for v in m["variants"]:
        builds.setdefault(build_key(v), build_text(v))
    build_order = sorted(builds, key=lambda k: (builds[k].split(" + ")[0], "patch" in builds[k], k))
    # (build, mode) -> variant_id
    vmap = {f'{build_key(v)}|{v["access_mode"]}': v["variant_id"] for v in m["variants"]}

    default_src, default_dst = (results[0]["comparison"]["baseline"],
                                results[0]["comparison"]["candidate"]) if results else ("", "")
    for r in results:
        if r["comparison"].get("primary"):
            default_src = r["comparison"]["baseline"]
            default_dst = r["comparison"]["candidate"]
            break
    sv, dv = variants.get(default_src, {}), variants.get(default_dst, {})

    # Access-mode options: each mode on its own, plus the cross-mode pairs that
    # actually exist, so "what does turning governance on cost me" is one choice
    # rather than two fiddly ones.
    mode_opts: list[tuple[str, str]] = [(f"{mo}|{mo}", access_label(mo)) for mo in modes]
    for a in modes:
        for b in modes:
            if a != b:
                mode_opts.append((f"{a}|{b}", f"{access_label(a)} \u2192 {access_label(b)}"))
    sel_mode = f'{sv.get("access_mode", "")}|{dv.get("access_mode", "")}'

    def rel_options(selected_v: dict) -> str:
        sel = build_key(selected_v) if selected_v else ""
        return "".join(
            f'<option value="{e(k)}"{" selected" if k == sel else ""}>{e(builds[k])}</option>'
            for k in build_order)

    parts.append('<h2 class="sec" id="compare">Choose what to compare</h2><div class="pickerbar">')
    parts.append('<div class="picker"><label>Source release</label>'
                 f'<select id="srcRel">{rel_options(sv)}</select></div>')
    parts.append('<div class="arrow">&rarr;</div>')
    parts.append('<div class="picker"><label>Candidate release</label>'
                 f'<select id="dstRel">{rel_options(dv)}</select></div>')
    parts.append('<div class="picker"><label>Access mode</label><select id="modeSel">'
                 + "".join(f'<option value="{e(val)}"'
                           f'{" selected" if val == sel_mode else ""}>{e(txt)}</option>'
                           for val, txt in mode_opts)
                 + "</select></div>")
    parts.append('<div class="picker"><label>Comparing</label>'
                 f'<span class="hint" id="selBadge">'
                 f'{e(build_text(sv) if sv else "")} {e(access_label(sv.get("access_mode")))}'
                 f' \u2192 {e(build_text(dv) if dv else "")} '
                 f'{e(access_label(dv.get("access_mode")))}</span></div>')
    declared = [r for r in results if r["comparison"].get("declared")]
    if declared:
        parts.append('<div class="quick"><span class="lbl">Recommended</span>')
        for r in declared:
            cls, _ = VERDICT_STYLE[r["verdict"]["level"]]
            b, c = variants[r["comparison"]["baseline"]], variants[r["comparison"]["candidate"]]
            parts.append(f'<button data-srcrel="{e(build_key(b))}" '
                         f'data-dstrel="{e(build_key(c))}" '
                         f'data-mode="{e(b["access_mode"])}|{e(c["access_mode"])}">'
                         f'<span class="dot {cls}" style="display:inline-block;margin-right:6px">'
                         f'</span>{e(r["comparison"]["title"])}</button>')
        parts.append("</div>")
    parts.append("</div>")
    parts.append(f'<p style="margin:8px 0 0;color:var(--ink3);font-size:12px">'
                 f'{len(results)} variant pair(s) precomputed — selection is instant and works '
                 f'offline. Not every release exists in every access mode; unavailable combinations '
                 f'say so.</p>')
    parts.append('<div class="nosel" id="noSelection" hidden></div>')
    # Escaping '<' prevents a value containing '</script>' from closing the
    # element early. Valid JSON either way: \\u003c is the same string to a parser.
    parts.append('<script id="vmap" type="application/json">'
                 + json.dumps(vmap).replace('<', '\\u003c')
                 + "</script>")

    for i, r in enumerate(results):
        cls, headline = VERDICT_STYLE[r["verdict"]["level"]]
        sid = r["comparison"]["comparison_id"]
        landing = (r["comparison"]["baseline"] == default_src
                   and r["comparison"]["candidate"] == default_dst)
        parts.append(f'<section class="cmp" id="{e(sid)}"{"" if landing else " hidden"}>')
        parts.append(f'<div class="verdict {cls}"><span class="lvl">{e(r["verdict"]["level"])}</span><div>'
                     f'<h3>{e(headline)} — {e(r["comparison"]["title"])}</h3><ul>')
        for reason in r["verdict"]["reasons"]:
            parts.append(f"<li>{e(reason)}</li>")
        parts.append(f'</ul><p style="margin:9px 0 0;font-size:12.5px;color:var(--ink2)">'
                     f'{chip(r["match"]["status"])} intent <code>{e(r["comparison"]["intent"])}</code> · '
                     f'{e(release_label(r["baseline"]["release_label"]))} '
                     f'{e(access_label(r["baseline"]["access_mode"]))} → '
                     f'{e(release_label(r["candidate"]["release_label"]))} '
                     f'{e(access_label(r["candidate"]["access_mode"]))}</p></div></div>')
        parts.append(hero(r))
        parts.append('<div data-anchor="correctness"></div>')
        parts.append(sec_correctness(r["correctness"]))
        parts.append('<div data-anchor="functional"></div>')
        parts.append(sec_functional(r["functional"], f"ftab-{i}"))
        parts.append('<div data-anchor="performance"></div>')
        # One section per performance workload, in declaration order, so scales
        # read 100g -> 1t -> 3t rather than being collapsed into whichever was
        # declared first.
        entries = r.get("performances") or (
            [{"workload_id": "", "label": "", "perf": r["performance"]}] if r["performance"] else [])
        multi = len(entries) > 1
        for k, entry in enumerate(entries):
            head = ("Performance" if not multi
                    else f'Performance — {entry["label"]}')
            parts.append(sec_perf(entry["perf"], r["match"],
                                  r["comparison"].get("intent", ""),
                                  f"{i}-{k}", head))
        parts.append('<div data-anchor="cost"></div>')
        parts.append(sec_cost(r["cost"], r["baseline"], r["candidate"], m["pricing"]))
        parts.append(sec_env(r))
        parts.append("</section>")

    # ---- Variant dashboard and the all-pairs matrix, below the results
    parts.append('<h2 class="sec" id="variants">Variants in this run</h2>')
    parts.append(dashboard(run, results))
    if len(variants) > 2:
        parts.append('<h3 class="sub">Verdict for every pair</h3>')
        parts.append('<div class="card pad">' + pair_matrix(results, variants) + "</div>")

    th = m["thresholds"]
    parts.append(
        '<footer id="method"><b>Method.</b> Performance uses best-of-N per query across '
        f'{th["min_iterations_for_perf_verdict"]}+ iterations, run serially within a variant and in '
        f'parallel across variants. Deltas inside the ±{th["perf_noise_band_pct"]:g}% noise band are '
        f'reported NEUTRAL, never as regressions; ≥{th["perf_regression_alert_pct"]:g}% raises severity. '
        'Aggregate uses the geometric mean of per-query ratios so one dominant query cannot carry the '
        'verdict. Functional results are diffed against the documented Lake Formation support matrix, so '
        'an operation AWS documents as unsupported is reported EXPECTED UNSUPPORTED rather than as a '
        'regression. Correctness is checked before performance: row counts, ordered result-set checksums, '
        'commit-log advancement and post-operation object listings. A comparison whose variants differ in '
        'more than one dimension is labelled UNMATCHED and its performance verdict is suppressed.'
        # The run manifest already records whether these numbers were measured or
        # generated. Hardcoding the sample-run wording here labelled real runs as
        # synthetic, contradicting the header two screens above.
        f'<br><br><b>Provenance.</b> {e(m.get("data_class_note", ""))}'
        f'<br><br><span style="color:var(--ink3)">'
        f'{"EMR Test Drive" if m.get("data_class") == "REAL" else "EMR Test Drive sample"}'
        f' · report schema v0.1 · {e(m["run_id"])}</span></footer>')
    parts.append(f"</div><script>{JS}</script></body></html>")
    return "".join(parts)


def render_json(run, results: list[dict]) -> str:
    out = {
        "run_id": run.manifest["run_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_class": run.manifest.get("data_class"),
        "report_schema": "0.1",
        "comparisons": [{
            "comparison_id": r["comparison"]["comparison_id"],
            "title": r["comparison"]["title"],
            "intent": r["comparison"]["intent"],
            "baseline": r["baseline"]["variant_id"],
            "candidate": r["candidate"]["variant_id"],
            "match": r["match"],
            "verdict": r["verdict"],
            "functional_counts": (r["functional"] or {}).get("counts", {}),
            "error_clusters": [{k: cl[k] for k in ("cluster_id", "signature", "count", "members")}
                               for cl in (r["functional"] or {}).get("clusters", [])],
            "correctness_findings": r["correctness"],
            "performance": (r["performance"] or {}).get("aggregate"),
            # Every scale, in declaration order. A consumer reading only
            # "performance" above would see the first workload alone.
            "performance_by_scale": [
                {"workload_id": pe["workload_id"], "label": pe["label"],
                 "aggregate": (pe["perf"] or {}).get("aggregate"),
                 "counts": (pe["perf"] or {}).get("counts", {})}
                for pe in (r.get("performances") or [])],
            "performance_counts": (r["performance"] or {}).get("counts", {}),
            "performance_units": [
                {k: u[k] for k in ("name", "verdict", "base_best_s", "cand_best_s", "delta_pct")}
                for u in (r["performance"] or {}).get("rows", [])],
            "cost": r["cost"],
        } for r in results],
    }
    return json.dumps(out, indent=2) + "\n"
