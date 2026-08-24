#!/usr/bin/env python3
"""Render the cashout reconciliation JSON (from cashout_report.py) as a
self-contained HTML report. All figures come straight from the JSON; the only
arithmetic here is Decimal sums/percentages for section subtotals."""
import json
import sys
from decimal import Decimal
from html import escape
from pathlib import Path

D = Decimal


def usd(x):
    d = D(str(x))
    sign = "-" if d < 0 else ""
    return f"{sign}${abs(d):,.2f}"


def k(n):
    return f"{n:,}K"


ELDORADO_ORDER_URL = "https://www.eldorado.gg/order/"

PASS_SHORT = [("P1", "1:1"), ("P2", "split"), ("P3", "combined"),
              ("P4b", "tolerant pair"), ("P4", "pooled"), ("P5", "amount+date"),
              ("manual", "user-confirmed")]
TIER = {
    "high":   ("high",   "✓", "Quantities and dates fit the strict rules; income is the exact wallet credit."),
    "check":  ("check",  "◆", "Matched, but an ambiguity was resolved by closest date — worth a glance."),
    "verify": ("verify", "!", "Matched with a tolerance or indirect evidence — verify before trusting."),
}


def pass_label(p):
    for pre, lab in PASS_SHORT:
        if p.startswith(pre):
            return lab
    return p


def badge(tier):
    cls, sym, _ = TIER[tier]
    return f'<span class="badge b-{cls}">{sym} {cls}</span>'


def transfers_table(ts):
    rows = "".join(
        f"<tr><td>{escape(t['created'][:16])}</td>"
        f"<td>{escape(t['name'])}</td>"
        f"<td class='sub'>{escape(t['email'] or '—')}</td>"
        f"<td class='num'>{t['delivered_k']:,} / {t['requested_k']:,}K</td>"
        f"<td class='num'>{usd(t['cost'])}</td>"
        f"<td>{escape(t['status'])}</td><td>{escape(t['platform'] or '—')}</td>"
        f"<td class='sub'>{escape(t['basis'])}</td></tr>"
        for t in ts)
    return ("<div class='tblwrap'><table><thead><tr><th>Transfer date</th><th>eatransfer name</th>"
            "<th>Customer email</th><th class='num'>Delivered / ordered</th><th class='num'>Cost</th>"
            "<th>Status</th><th>Plat.</th><th>Identity basis</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")


def order_link(oid, text):
    return (f"<a href='{ELDORADO_ORDER_URL}{escape(oid)}' target='_blank' "
            f"rel='noopener'>{text}</a>")


def orders_table(os_):
    rows = "".join(
        f"<tr><td>{escape(o['created'][:16])}</td>"
        f"<td class='mono sub'>{order_link(o['id'], escape(o['id'][:8]) + '… ↗')}</td>"
        f"<td class='num'>{o['qty_k']:,}K</td>"
        f"<td class='num'>{usd(o['buyer_paid'])}</td>"
        f"<td class='num'>{usd(o['credited'])}</td>"
        f"<td>{escape(o['platform'] or '—')}</td><td>{escape(o['state'])}</td></tr>"
        for o in os_)
    return ("<div class='tblwrap'><table><thead><tr><th>Sold</th><th>Order</th>"
            "<th class='num'>Qty</th><th class='num'>Buyer paid</th><th class='num'>Credited to me</th>"
            "<th>Plat.</th><th>State</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")


def group_row(g):
    checks = "".join(f"<li class='ok'>{escape(c)}</li>" for c in g["checks"])
    warns = "".join(f"<li class='warn'>{escape(w)}</li>" for w in g["warnings"])
    profit_cls = "neg" if D(str(g["profit"])) < 0 else ""
    qty_sent = f"{g['qty_transferred_k']:,}K" if g["transfers"] else "—"
    tr_block = (f"<h4>eatransfer transfer{'s' if len(g['transfers']) > 1 else ''}</h4>"
                + transfers_table(g["transfers"]) if g["transfers"] else
                "<p class='sub'>No eatransfer transfer — you confirmed this order was "
                "fulfilled outside eatransfer, so its cost here is $0.00.</p>")
    return f"""<details class="grp">
<summary>
  <span class="c-date">{escape(g['orders'][0]['created'][:10])}</span>
  <span class="c-buyer">{escape(g['buyer'])}</span>
  <span class="c-badge">{badge(g['confidence'])}</span>
  <span class="c-pass">{escape(pass_label(g['pass']))}</span>
  <span class="c-qty num">{g['qty_sold_k']:,} → {qty_sent}</span>
  <span class="c-inc num">{usd(g['income'])}</span>
  <span class="c-cost num">{usd(g['cost'])}</span>
  <span class="c-prof num {profit_cls}">{usd(g['profit'])}</span>
</summary>
<div class="grp-body">
  <p class="grp-pass">Matched by pass: {escape(g['pass'])}</p>
  <h4>El Dorado order{'s' if len(g['orders']) > 1 else ''}</h4>{orders_table(g['orders'])}
  {tr_block}
  <h4>Validation</h4><ul class="vlist">{checks}{warns}</ul>
</div>
</details>"""


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "data/cashout_2026-07-16.json")
    rep = json.loads(src.read_text())
    c, s = rep["cashout"], rep["summary"]

    my_tags = rep["config"].get("my_tags", [])
    tag = "/".join(my_tags) or "own-tag"
    suffix_rule = " / ".join(f"_{t} / -{t}" for t in my_tags) or "(no tag configured)"
    seller = rep["config"].get("seller_name", "")
    eyebrow = "El Dorado → eatransfer" + (f" · seller {seller}" if seller else "")
    fee_note = (f"{rep['config'].get('withdrawal_method', 'withdrawal')}: "
                f"{D(rep['config'].get('withdrawal_percent', '0')):.0%} + "
                f"${D(rep['config'].get('withdrawal_flat_usd', '0')):,.0f}")

    inc_parts = [("high", "Solid matches", D(s["income_high"])),
                 ("check", "Check-flagged", D(s["income_check"])),
                 ("verify", "Verify-flagged", D(s["income_verify"])),
                 ("unmatched", "No cost found", D(s["income_unmatched"]))]
    cost_parts = [("cost", "Matched transfer costs", D(s["cost_matched"])),
                  ("unatt", f"Unattached {tag} costs", D(s["cost_unattached"]))]
    amount = D(c["amount"])
    inc_matched = D(s["income_high"]) + D(s["income_check"]) + D(s["income_verify"])
    pct_matched = inc_matched / amount * 100
    total_cost = D(s["cost_matched"]) + D(s["cost_unattached"])
    extra_profit = D(s["profit_full_estimate"]) - D(s["profit_matched"])

    def seg_html(parts):
        out = []
        for key, label, val in parts:
            if val <= 0:
                continue
            w = float(val / amount * 100)
            out.append(f"<div class='seg s-{key}' style='width:{w:.3f}%' "
                       f"title='{escape(label)}: {usd(val)}'></div>")
        return "".join(out)

    def legend_html(parts):
        return "".join(
            f"<span class='lg'><i class='sw s-{key}'></i>{escape(label)} "
            f"<b>{usd(val)}</b></span>"
            for key, label, val in parts if val != 0)

    groups = rep["groups"]
    attention = [g for g in groups if g["confidence"] != "high"]
    clean = [g for g in groups if g["confidence"] == "high"]
    att_inc = sum(D(str(g["income"])) for g in attention)
    att_cost = sum(D(str(g["cost"])) for g in attention)
    att_prof = sum(D(str(g["profit"])) for g in attention)

    header_cols = ("<div class='grp head'><span class='c-date'>Sold</span>"
                   "<span class='c-buyer'>Buyer</span><span class='c-badge'>Tier</span>"
                   "<span class='c-pass'>How matched</span><span class='c-qty num'>Sold → sent</span>"
                   "<span class='c-inc num'>Income</span><span class='c-cost num'>Cost</span>"
                   "<span class='c-prof num'>Profit</span></div>")

    view_lbl = "<span class='sub'>view order ↗</span>"
    unmatched_rows = "".join(
        f"<tr><td>{escape(o['created'][:10])}</td>"
        f"<td><b>{escape(o['buyer'])}</b><br>{order_link(o['id'], view_lbl)}</td>"
        f"<td class='num'>{o['qty_k']:,}K</td><td class='num'>{usd(o['buyer_paid'])}</td>"
        f"<td class='num'>{usd(o['credited'])}</td>"
        f"<td class='sub'>{'<br>'.join(escape(n) for n in o['near_misses']) or '— none within ±25% / 4 days'}</td></tr>"
        for o in rep["unmatched_orders"])
    unmatched_inc = D(s["income_unmatched"])
    if rep["unmatched_orders"]:
        unmatched_section = f"""<section>
  <h2>Orders with no transfer found <span class="count">— {len(rep['unmatched_orders'])} orders, {usd(unmatched_inc)} income</span></h2>
  <p class="sect-sub">Income for these is in your pocket (it is part of the cashout) but no {escape(tag)} or linkable
  eatransfer transfer was found — likely fulfilled outside eatransfer, or under a foreign tag
  (nearest candidates shown; foreign-tag transfers are omitted per your rule).</p>
  <div class="grouptables"><div class="tblwrap"><table>
    <thead><tr><th>Sold</th><th>Buyer</th><th class="num">Qty</th><th class="num">Buyer paid</th>
    <th class="num">Credited</th><th>Nearest candidates (omitted / other)</th></tr></thead>
    <tbody>{unmatched_rows}</tbody></table></div></div>
</section>"""
    else:
        unmatched_section = """<section>
  <h2>Orders with no transfer found <span class="count">— none</span></h2>
  <p class="sect-sub">Every order in this cashout is either matched to its eatransfer transfers or
  confirmed by you as fulfilled outside eatransfer (those appear above as user-confirmed matches
  with $0.00 cost). Income reconciliation is complete.</p>
</section>"""
    split_tiles = "".join(
        f"""  <div class="tile split">
    <p class="lbl">{escape(name)}'s share ({float(D(s['net_split_pct'][name])) * 100:.0f}% of net)</p>
    <p class="val">{usd(amt)}</p>
    <p class="note">of {usd(s['net_after_fee_est'])} net after fee</p>
  </div>
""" for name, amt in s.get("net_split", {}).items())

    if rep["unmatched_orders"]:
        hero_note = (f"up to {usd(s['profit_full_estimate'])} if the {len(rep['unmatched_orders'])} unmatched "
                     f"orders truly had no transfer cost (+{usd(extra_profit)})")
    else:
        hero_note = (f"all {c['orders_in_window']} orders reconciled; full-cashout estimate "
                     f"{usd(s['profit_full_estimate'])} after {usd(s['cost_unattached'])} unattached {tag} costs")

    def flat_transfers(ts, extra_col=None):
        rows = []
        for t in ts:
            extra = f"<td class='sub'>{escape(t.get(extra_col, ''))}</td>" if extra_col else ""
            rows.append(
                f"<tr><td>{escape(t['created'][:10])}</td><td>{escape(t['name'])}</td>"
                f"<td class='sub'>{escape(t['email'] or '—')}</td>"
                f"<td class='num'>{t['delivered_k']:,} / {t['requested_k']:,}K</td>"
                f"<td class='num'>{usd(t['cost'])}</td>"
                f"<td class='sub'>{escape(t['basis'])}</td>{extra}</tr>")
        return "".join(rows)

    html = f"""<title>Cashout reconciliation — {escape(c['date'])}</title>
<style>
:root {{
  --page:#f6f7f4; --card:#fdfdfc; --ink:#171a14; --ink2:#565c50; --mut:#8b9184;
  --line:#e3e5dd; --hair:rgba(23,26,20,.10); --acc:#31684d; --acc-ink:#274f3c;
  --good:#0ca30c; --good-t:#e7f4e4; --warn:#fab219; --warn-t:#fdf3dc;
  --ser:#ec835a; --ser-t:#fbeae3; --crit:#d03b3b; --crit-t:#f9e5e3;
  --cost:#31684d; --cost-t:#e4efe8; --unatt:#7a8f83;
  --neg:#b02a2a; --track:#ecede8;
}}
@media (prefers-color-scheme: dark) {{ :root {{
  --page:#0e100d; --card:#181b16; --ink:#eef0e9; --ink2:#b7bdad; --mut:#878d80;
  --line:#2b2e28; --hair:rgba(238,240,233,.12); --acc:#7bbd98; --acc-ink:#93cdaa;
  --good:#0ca30c; --good-t:#12290f; --warn:#fab219; --warn-t:#33270a;
  --ser:#ec835a; --ser-t:#331b10; --crit:#e05252; --crit-t:#341111;
  --cost:#7bbd98; --cost-t:#15251c; --unatt:#5d7265;
  --neg:#e05252; --track:#23261f;
}} }}
:root[data-theme="dark"] {{
  --page:#0e100d; --card:#181b16; --ink:#eef0e9; --ink2:#b7bdad; --mut:#878d80;
  --line:#2b2e28; --hair:rgba(238,240,233,.12); --acc:#7bbd98; --acc-ink:#93cdaa;
  --good:#0ca30c; --good-t:#12290f; --warn:#fab219; --warn-t:#33270a;
  --ser:#ec835a; --ser-t:#331b10; --crit:#e05252; --crit-t:#341111;
  --cost:#7bbd98; --cost-t:#15251c; --unatt:#5d7265;
  --neg:#e05252; --track:#23261f;
}}
:root[data-theme="light"] {{
  --page:#f6f7f4; --card:#fdfdfc; --ink:#171a14; --ink2:#565c50; --mut:#8b9184;
  --line:#e3e5dd; --hair:rgba(23,26,20,.10); --acc:#31684d; --acc-ink:#274f3c;
  --good:#0ca30c; --good-t:#e7f4e4; --warn:#fab219; --warn-t:#fdf3dc;
  --ser:#ec835a; --ser-t:#fbeae3; --crit:#d03b3b; --crit-t:#f9e5e3;
  --cost:#31684d; --cost-t:#e4efe8; --unatt:#7a8f83;
  --neg:#b02a2a; --track:#ecede8;
}}
* {{ box-sizing:border-box; }}
body {{ background:var(--page); color:var(--ink); margin:0;
  font:15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }}
.wrap {{ max-width:1060px; margin:0 auto; padding:40px 24px 80px; }}
.mono {{ font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.92em; }}
.num {{ font-variant-numeric: tabular-nums; text-align:right; }}
.sub {{ color:var(--ink2); font-size:13px; }}
.neg {{ color:var(--neg); }}
a {{ color:var(--acc-ink); }}

header .eyebrow {{ text-transform:uppercase; letter-spacing:.14em; font-size:11.5px;
  font-weight:600; color:var(--acc-ink); margin:0 0 10px; }}
header h1 {{ font-size:30px; line-height:1.15; margin:0 0 10px; letter-spacing:-.01em; text-wrap:balance; }}
header .meta {{ color:var(--ink2); margin:0; max-width:70ch; }}
header .meta b {{ color:var(--ink); }}

.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(158px,1fr));
  gap:12px; margin:30px 0 12px; }}
.tile {{ background:var(--card); border:1px solid var(--hair); border-radius:8px; padding:14px 16px; }}
.tile .lbl {{ font-size:12.5px; color:var(--ink2); margin:0 0 4px; }}
.tile .val {{ font-size:23px; font-weight:650; letter-spacing:-.01em; margin:0; }}
.tile .note {{ font-size:12px; color:var(--mut); margin:3px 0 0; }}
.tile.hero {{ grid-column:span 2; border-left:3px solid var(--acc); }}
.tile.hero .val {{ font-size:34px; }}
.tile.split {{ border-top:3px solid var(--acc); }}
.tile.split .val {{ color:var(--acc-ink); }}

.flow {{ background:var(--card); border:1px solid var(--hair); border-radius:8px;
  padding:18px 20px 16px; margin:12px 0 40px; }}
.flow h3 {{ margin:0 0 14px; font-size:14px; }}
.bar-lbl {{ font-size:12.5px; color:var(--ink2); margin:10px 0 5px;
  display:flex; justify-content:space-between; }}
.bar {{ display:flex; gap:2px; height:22px; border-radius:4px; overflow:hidden; background:var(--track); }}
.seg {{ height:100%; }}
.s-high {{ background:var(--good); }} .s-check {{ background:var(--warn); }}
.s-verify {{ background:var(--ser); }} .s-unmatched {{ background:var(--crit); }}
.s-cost {{ background:var(--cost); }} .s-unatt {{ background:var(--unatt); }}
.legend {{ display:flex; flex-wrap:wrap; gap:8px 20px; margin-top:12px; font-size:13px; color:var(--ink2); }}
.legend b {{ color:var(--ink); font-variant-numeric:tabular-nums; }}
.sw {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; }}
.flow .profit-note {{ margin:14px 0 0; font-size:13px; color:var(--ink2); }}

section {{ margin:0 0 44px; }}
section > h2 {{ font-size:19px; margin:0 0 4px; letter-spacing:-.005em; }}
section > .sect-sub {{ color:var(--ink2); font-size:13.5px; margin:0 0 16px; max-width:80ch; }}
.count {{ color:var(--mut); font-weight:500; }}

.badge {{ display:inline-flex; align-items:center; gap:4px; font-size:11px; font-weight:650;
  text-transform:uppercase; letter-spacing:.05em; padding:2px 8px; border-radius:99px; white-space:nowrap; }}
.b-high {{ background:var(--good-t); color:var(--good); }}
.b-check {{ background:var(--warn-t); color:#8a6205; }}
.b-verify {{ background:var(--ser-t); color:#b0522e; }}
@media (prefers-color-scheme: dark) {{
  .b-check {{ color:var(--warn); }} .b-verify {{ color:var(--ser); }}
}}
:root[data-theme="dark"] .b-check {{ color:var(--warn); }}
:root[data-theme="dark"] .b-verify {{ color:var(--ser); }}
:root[data-theme="light"] .b-check {{ color:#8a6205; }}
:root[data-theme="light"] .b-verify {{ color:#b0522e; }}

.grp {{ border-bottom:1px solid var(--line); }}
.grp summary {{ list-style:none; cursor:pointer; }}
.grp summary::-webkit-details-marker {{ display:none; }}
.grp summary, .grp.head {{ display:grid; align-items:center; gap:10px;
  grid-template-columns:82px minmax(130px,1.4fr) 86px minmax(86px,.9fr) 120px 84px 84px 84px;
  padding:8px 10px; font-size:13.5px; }}
.grp.head {{ color:var(--mut); font-size:11.5px; text-transform:uppercase;
  letter-spacing:.07em; border-bottom:1px solid var(--line); font-weight:600; }}
.grp summary:hover {{ background:var(--track); }}
.grp summary:focus-visible {{ outline:2px solid var(--acc); outline-offset:-2px; }}
.c-buyer {{ font-weight:600; overflow-wrap:anywhere; }}
.c-pass {{ color:var(--ink2); }}
.c-date {{ color:var(--ink2); font-variant-numeric:tabular-nums; }}
.grp-body {{ padding:6px 10px 20px; border-top:1px dashed var(--line); background:var(--card); }}
.grp-body h4 {{ margin:14px 0 6px; font-size:12px; text-transform:uppercase;
  letter-spacing:.08em; color:var(--mut); }}
.grp-pass {{ font-size:13px; color:var(--ink2); margin:8px 0 0; }}
.vlist {{ margin:0; padding:0 0 0 2px; list-style:none; font-size:13.5px; }}
.vlist li {{ padding:2px 0 2px 22px; position:relative; }}
.vlist li.ok::before {{ content:"✓"; position:absolute; left:0; color:var(--good); font-weight:700; }}
.vlist li.warn::before {{ content:"!"; position:absolute; left:2px; color:var(--ser); font-weight:800; }}
.vlist li.warn {{ color:var(--ink); }}

.tblwrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:13.5px; color:inherit; font-family:inherit; }}
th {{ text-align:left; font-size:11.5px; text-transform:uppercase; letter-spacing:.07em;
  color:var(--mut); font-weight:600; padding:6px 10px; border-bottom:1px solid var(--line); }}
th.num {{ text-align:right; }}
td {{ padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
tr:last-child td {{ border-bottom:none; }}
.grouptables {{ background:var(--card); border:1px solid var(--hair); border-radius:8px; }}
.totals {{ display:flex; gap:26px; flex-wrap:wrap; padding:10px 10px; font-size:13.5px;
  color:var(--ink2); border-top:2px solid var(--line); }}
.totals b {{ color:var(--ink); font-variant-numeric:tabular-nums; }}

.method {{ background:var(--card); border:1px solid var(--hair); border-radius:8px;
  padding:20px 22px; font-size:13.5px; color:var(--ink2); }}
.method h2 {{ font-size:15px; color:var(--ink); margin:0 0 10px; }}
.method dt {{ font-weight:600; color:var(--ink); margin-top:10px; }}
.method dd {{ margin:2px 0 0 0; }}
.method ul {{ padding-left:18px; margin:6px 0; }}
@media (max-width:720px) {{
  .grp summary, .grp.head {{ grid-template-columns:72px 1fr 80px 80px; }}
  .c-pass, .c-qty, .c-inc, .c-cost {{ display:none; }}
  .tile.hero {{ grid-column:1 / -1; }}
}}
@media (prefers-reduced-motion: no-preference) {{
  .grp summary {{ transition: background .12s ease; }}
}}
</style>

<div class="wrap">
<header>
  <p class="eyebrow">{escape(eyebrow)}</p>
  <h1>Cashout reconciliation — {usd(c['amount'])} withdrawn {escape(c['date'])}</h1>
  <p class="meta">Every dollar of the <b>{escape(c['type'])}</b> withdrawal is traced below.
  It covers sale income credited between <b>{escape(c['window_start'])}</b> and
  <b>{escape(c['window_end'])} UTC</b> — {c['orders_in_window']} El Dorado orders, verified to
  sum to the withdrawal to the cent. Each order was matched to its eatransfer transfer cost
  across {s['matched_transfers']} transfers; anything that could not be matched under strict rules
  is listed with the evidence, never guessed.</p>
</header>

<div class="kpis">
  <div class="tile hero">
    <p class="lbl">Gross profit — matched &amp; validated</p>
    <p class="val">{usd(s['profit_matched'])}</p>
    <p class="note">{hero_note}</p>
  </div>
  <div class="tile">
    <p class="lbl">Income reconciled</p>
    <p class="val">{usd(inc_matched)}</p>
    <p class="note">≈{pct_matched:.1f}% of the cashout</p>
  </div>
  <div class="tile">
    <p class="lbl">Transfer costs identified</p>
    <p class="val">{usd(total_cost)}</p>
    <p class="note">{usd(s['cost_matched'])} matched + {usd(s['cost_unattached'])} unattached</p>
  </div>
  <div class="tile">
    <p class="lbl">Withdrawal fee (est.)</p>
    <p class="val">{usd(s['withdrawal_fee_est'])}</p>
    <p class="note">{escape(fee_note)}</p>
  </div>
  <div class="tile">
    <p class="lbl">Net after fee (est.)</p>
    <p class="val">{usd(s['net_after_fee_est'])}</p>
    <p class="note">full-cashout estimate − fee</p>
  </div>
{split_tiles}</div>

<div class="flow">
  <h3>Where the {usd(c['amount'])} came from — and what it cost</h3>
  <p class="bar-lbl"><span>Income (wallet credits in the window)</span><span>{usd(c['window_income'])}</span></p>
  <div class="bar">{seg_html(inc_parts)}</div>
  <p class="bar-lbl"><span>Costs (eatransfer transfers, same scale)</span><span>{usd(total_cost)}</span></p>
  <div class="bar">{seg_html(cost_parts)}</div>
  <div class="legend">{legend_html(inc_parts)}{legend_html(cost_parts)}</div>
  <p class="profit-note">The unfilled part of the cost bar is the gross margin. Foreign-tag transfers
  ({rep['foreign_count']} totaling {usd(rep['foreign_total_cost'])}) are other people's and excluded entirely.</p>
</div>

<section>
  <h2>Needs your eye <span class="count">— {len(attention)} matches + {len(rep['unmatched_orders'])} unmatched orders</span></h2>
  <p class="sect-sub">Matches below were made with a tolerance or a tie-break — each row expands to show
  the exact orders, transfers and the validation that was applied. Nothing here is silently assumed.</p>
  <div class="grouptables">
    {header_cols}
    {''.join(group_row(g) for g in attention)}
    <div class="totals"><span>Subtotal</span><span>income <b>{usd(att_inc)}</b></span>
    <span>cost <b>{usd(att_cost)}</b></span><span>profit <b>{usd(att_prof)}</b></span></div>
  </div>
</section>

{unmatched_section}

<section>
  <h2>Clean matches <span class="count">— {len(clean)} groups, every check passed</span></h2>
  <p class="sect-sub">Strict quantity window (−{rep['config']['quantity_tolerance_k']}K … +5%+{rep['config']['quantity_tolerance_k']}K for EA trade tax),
  transfer within {rep['config']['match_window_days']} days of the sale, income taken from exact wallet credits. Click any row for the full trail.</p>
  <div class="grouptables">
    {header_cols}
    {''.join(group_row(g) for g in clean)}
    <div class="totals"><span>Subtotal</span><span>income <b>{usd(s['income_high'])}</b></span>
    <span>cost <b>{usd(s['cost_high'])}</b></span>
    <span>profit <b>{usd(D(s['income_high']) - D(s['cost_high']))}</b></span></div>
  </div>
</section>

<section>
  <h2>Unattached {escape(tag)} costs <span class="count">— {len(rep['unattached_transfers'])} transfers, {usd(s['cost_unattached'])}</span></h2>
  <p class="sect-sub">Carry your tag and sit inside the window, but match no order — mostly extra partial
  sends around split deliveries. Counted against this cashout's profit estimate.</p>
  <div class="grouptables"><div class="tblwrap"><table>
    <thead><tr><th>Date</th><th>Name</th><th>Email</th><th class="num">Delivered / ordered</th>
    <th class="num">Cost</th><th>Identity basis</th></tr></thead>
    <tbody>{flat_transfers(rep['unattached_transfers'])}</tbody></table></div></div>
</section>

<section>
  <h2>Excluded — with reasons</h2>
  <p class="sect-sub">Shown so the picture is complete; none of these touch this cashout's numbers.</p>
  <div class="grouptables"><div class="tblwrap"><table>
    <thead><tr><th>Date</th><th>Name</th><th>Email</th><th class="num">Delivered / ordered</th>
    <th class="num">Cost</th><th>Identity basis</th><th>Why excluded</th></tr></thead>
    <tbody>
    {flat_transfers(rep['other_payout_transfers'], 'matches_order')}
    {"".join(f"<tr><td>{escape(t['created'][:10])}</td><td>{escape(t['name'])}</td><td class='sub'>{escape(t['email'] or '—')}</td><td class='num'>{t['delivered_k']:,} / {t['requested_k']:,}K</td><td class='num'>{usd(t['cost'])}</td><td class='sub'>{escape(t['basis'])}</td><td class='sub'>unattached {escape(tag)} transfer dated outside this cashout window — another payout's era, not counted here</td></tr>" for t in rep.get('unattached_other_windows_transfers', []))}
    {"".join(f"<tr><td>{escape(t['created'][:10])}</td><td>{escape(t['name'])}</td><td class='sub'>{escape(t['email'] or '—')}</td><td class='num'>{t['delivered_k']:,} / {t['requested_k']:,}K</td><td class='num'>{usd(t['cost'])}</td><td class='sub'>{escape(t['basis'])}</td><td class='sub'>non-{escape(tag)} suffix on your buyer's name — omitted per your rule; flag if it's a typo of {escape(tag)}</td></tr>" for t in rep['suspicious_transfers'])}
    {"".join(f"<tr><td>{escape(t['created'][:10])}</td><td>{escape(t['name'])}</td><td class='sub'>{escape(t['email'] or '—')}</td><td class='num'>{t['delivered_k']:,} / {t['requested_k']:,}K</td><td class='num'>{usd(t['cost'])}</td><td class='sub'>{escape(t['basis'])}</td><td class='sub'>no tag, no name/email link to any of your buyers</td></tr>" for t in rep['unlinked_transfers'])}
    </tbody></table></div></div>
  <p class="sect-sub" style="margin-top:12px">Also excluded: <b>{rep['foreign_count']} foreign-tag transfers</b>
  ({usd(rep['foreign_total_cost'])}, tags {escape(', '.join(rep['config']['detected_foreign_tags']))}) and
  {rep['zero_transfers_ignored']} zero-value rows (0K delivered, $0 cost — failed attempts).</p>
</section>

<section class="method">
  <h2>How this was matched</h2>
  <p>Data fetched live from eldorado.gg (orders + wallet payments) and eatransfer.top (orders + full
  archive) on {escape(c['date'])}. Matching and every figure computed by
  <span class="mono">scripts/cashout_report.py</span>; income is always the exact wallet credit, never a formula.
  The engine asserts that the four income buckets sum exactly to the withdrawal, and that no order or
  transfer appears twice.</p>
  <dl>
    <dt>✓ high — {TIER['high'][2]}</dt>
    <dt>◆ check — {TIER['check'][2]}</dt>
    <dt>! verify — {TIER['verify'][2]}</dt>
  </dl>
  <ul>
    <li><b>1:1</b> — one transfer fits one order (quantity within −{rep['config']['quantity_tolerance_k']}K…+5% and ≤{rep['config']['match_window_days']} days apart).</li>
    <li><b>split</b> — several partial transfers to the same customer sum to one order (unique combination).</li>
    <li><b>combined</b> — one transfer covers several of the same buyer's orders (unique combination).</li>
    <li><b>tolerant pair</b> — same buyer, quantity within ±15% or up to 14 days apart; always flagged <i>verify</i>.</li>
    <li><b>amount+date</b> — customer name matches no buyer; linked by unique quantity + date (≤2 days) or by an
    email already seen on your confirmed transfers; always flagged <i>verify</i>.</li>
    <li><b>user-confirmed</b> — a manual override you supplied (<span class="mono">data/manual_matches.json</span>);
    the stated reason is the first line of the validation trail.</li>
  </ul>
  <p>Suffix rule applied: <b>{escape(suffix_rule)}</b> (any case) = yours; any other suffix = omitted;
  no suffix = linked via exact buyer name or customer email before being counted.</p>
</section>
</div>"""
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "data/reports/cashout-2026-07-16.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    # entity-encode non-ASCII so the page renders right regardless of charset headers
    out.write_text(html.encode("ascii", "xmlcharrefreplace").decode("ascii"))
    print(f"wrote {out} ({len(html)//1024}KB)")


if __name__ == "__main__":
    main()
