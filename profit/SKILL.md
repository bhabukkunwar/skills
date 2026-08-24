---
name: profit
description: Reconcile El Dorado FC coin sales against eatransfer purchase costs and produce a profit report. Use when the user asks for a profit report, reconciliation, or to check earnings.
---

# Profit reconciliation: El Dorado vs eatransfer

You fetch fresh order data from both platforms with the Playwright MCP browser,
then run the deterministic script that does ALL matching and money math.

**You never calculate, estimate, or match orders yourself. The script does that.
Your job is only: fetch data, run script, show its output.**

The default deliverable is the **per-cashout HTML report written to
`data/reports/` locally** (steps 6–8), reconciling the most recent El Dorado
withdrawal. The plain `reconcile.py` text output (step 5) is a quick
overview / sanity pass along the way.

## Step 1: load Playwright tools

Use ToolSearch with `select:mcp__playwright__browser_navigate,mcp__playwright__browser_evaluate`.
If Playwright MCP is not connected, tell the user to run
`claude mcp add playwright -- npx @playwright/mcp@latest` and restart the session.

## Step 2: fetch El Dorado data

Navigate to `https://www.eldorado.gg/dashboard/wallet`.
If the page shows a "Log in" button or redirects away from the dashboard, the session
expired: ask the user to log in inside the Playwright browser window, wait for them
to confirm, then navigate again.

Run browser_evaluate with `filename: "eldorado_orders_full.json"`:

```js
async () => {
  const all = [];
  let cursor = '9999-99-99 99:99:99.999999999999999-9999-9999-9999-999999999999';
  for (let i = 0; i < 1000; i++) {
    const u = `/api/orders/me/seller/orders?cursorValue=${encodeURIComponent(cursor)}&pageSize=20&pageDirection=Next&isAscendingDateOrder=false&ignorePendingReviewOrders=true&displayFilter=DisplaySellingOrders&orderGroup=Regular`;
    const r = await fetch(u, {credentials:'include'});
    if (!r.ok) throw new Error('HTTP '+r.status+' on page '+i);
    const j = await r.json();
    const res = j.results || [];
    all.push(...res);
    if (!j.nextPageCursor || res.length === 0) break;
    cursor = j.nextPageCursor;
  }
  return {count: all.length, results: all};
}
```

Then the same with `filename: "eldorado_payments_full.json"`, replacing the URL line with:

```js
    const u = `/api/userpayment/me/payments?paymentsCategory=All&cursorValue=${encodeURIComponent(cursor)}&pageSize=30&pageDirection=Next`;
```

(page size limits are enforced server-side: orders max 20, payments max 30)

## Step 3: fetch eatransfer data

Navigate to `https://eatransfer.top/orders.php`.
If the URL redirects to `futtransfer.eu.auth0.com`, ask the user to log in inside
the Playwright browser window, wait for confirmation, then navigate again.

Run browser_evaluate with `filename: "eatransfer_orders_full.json"`:

```js
async () => {
  const a = await (await fetch('/getOrders.php', {credentials:'include'})).json();
  const b = await (await fetch('/getOrders.php?override=0&archived=1&useOwnQuery=0', {credentials:'include'})).json();
  return {activeCount: (a.data||[]).length, archivedCount: (b.data||[]).length, active: a.data||[], archived: b.data||[]};
}
```

### Step 3b: extend the archive dump if needed

`getOrders.php?archived=1` only returns ~2 recent days and ignores date params.
Older transfers come from `data/raw/eatransfer_archive_range.json`, which is
cumulative — check the "archive range dump: ... (START .. END)" banner that
reconcile.py prints (or the reconciliation window you are about to run) and if
there is a gap between the dump's END and the ~2 days the archived endpoint
covers, fetch the missing range and APPEND it.

Run browser_evaluate with `filename: "eatransfer_archive_range_new.json"`, setting
`batches` to weekly [start, end] pairs covering the gap (one-day overlaps at the
seams are fine — everything is deduped by uuid downstream):

```js
async () => {
  const batches = [
    ['2026-07-15','2026-07-22'], ['2026-07-22','2026-07-29'], // ...adjust to the gap
  ];
  const rows = [], exp = [];
  for (const [ds, de] of batches) {
    // DataTables endpoint, server-side paged
    let start = 0, total = Infinity;
    while (start < total) {
      const p = new URLSearchParams();
      p.set('draw','1'); p.set('start', String(start)); p.set('length','500');
      p.set('search[value]',''); p.set('search[regex]','false');
      p.set('order[0][column]','3'); p.set('order[0][dir]','asc');
      for (let c = 0; c < 15; c++) {
        p.set(`columns[${c}][data]`, String(c));
        p.set(`columns[${c}][searchable]`,'true');
        p.set(`columns[${c}][orderable]`,'true');
        p.set(`columns[${c}][search][value]`,'');
        p.set(`columns[${c}][search][regex]`,'false');
      }
      p.set('dateStart',ds); p.set('dateEnd',de);
      p.set('associatedUsers','0'); p.set('display','allOrders');
      p.set('orderMode','all'); p.set('platformFilter','all');
      p.set('riskLevelFilter','all'); p.set('onlyOwn','0');
      const r = await fetch('/getOrdersArchive.php', {method:'POST', credentials:'include', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body: p});
      if (!r.ok) throw new Error('archive HTTP '+r.status+' for '+ds);
      const j = await r.json();
      total = j.recordsFiltered;
      const got = j.data || [];
      rows.push(...got);
      if (got.length === 0) break;
      start += got.length;
    }
    // export endpoint (clean name/email)
    const b = new URLSearchParams({dateStart:ds, dateEnd:de, associatedUsers:'0', display:'allOrders', onlyOwn:'0', format:'json'});
    const r2 = await fetch('/getOrdersArchiveExport.php', {method:'POST', credentials:'include', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body: b});
    if (!r2.ok) throw new Error('export HTTP '+r2.status+' for '+ds);
    const j2 = await r2.json();
    exp.push(...(j2.data || []));
  }
  return {rowCount: rows.length, exportCount: exp.length, rows, export: exp};
}
```

Then merge it into the cumulative dump (concatenate `rows`/`export`; the parsers
dedup by uuid):

```bash
python3 - <<'EOF'
import json
from pathlib import Path
src = Path('eatransfer_archive_range_new.json')
if not src.exists():
    src = Path('.playwright-mcp/eatransfer_archive_range_new.json')
new = json.loads(src.read_text())
tgt = Path('data/raw/eatransfer_archive_range.json')
old = json.loads(tgt.read_text())
old['rows'].extend(new['rows']); old['export'].extend(new['export'])
old['rowCount'] = len(old['rows']); old['exportCount'] = len(old['export'])
tgt.write_text(json.dumps(old)); src.unlink()
print('merged rows/export:', len(old['rows']), len(old['export']))
EOF
```

## Step 4: move files and run the script

The evaluate results are saved as JSON files in the project root (or `.playwright-mcp/`).
Move all three into `data/raw/`, overwriting previous dumps:

```bash
for f in eldorado_orders_full.json eldorado_payments_full.json eatransfer_orders_full.json; do
  [ -f "$f" ] && mv -f "$f" data/raw/ || { [ -f ".playwright-mcp/$f" ] && mv -f ".playwright-mcp/$f" data/raw/; }
done
```

Then run:

```bash
python3 scripts/reconcile.py
```

(`--since YYYY-MM-DD` limits which El Dorado orders are considered, if the user asked
for a specific period.)

## Step 5: quick overview

Summarize the reconcile.py output for the user (headline totals + every CHECK
line; NEVER change, recompute, or round any number). If the script exits with an
error, show the error and stop — do not improvise numbers.

Matched pairs are also written to `data/matched.csv`.

## Step 6: per-cashout reconciliation (the real report)

The user reconciles per cashout. Pick the most recent withdrawal from
reconcile.py's "WITHDRAWALS ON RECORD" section (or the one the user asked for)
and run the multi-pass matcher (handles split/combined/pooled transfers,
missing tags, email-linked identities; asserts income buckets sum exactly to
the withdrawal):

```bash
python3 scripts/cashout_report.py --cashout YYYY-MM-DD --json data/cashout_YYYY-MM-DD.json
```

Cost accounting rules the script enforces (explain these when the user asks
why a number did or didn't move):

- **Unattached own-tag costs** = transfers carrying the user's tag (config
  `my_tags`) that match no order.
  Only those dated INSIDE the cashout window (previous withdrawal → this one)
  count toward this cashout's cost; earlier/later ones are listed under
  "another payout's era" and excluded (prevents double-counting once the
  archive spans several cashouts).
- Matching moves dollars between buckets, it does not create profit: income is
  fixed at the withdrawal amount and confirming a match just relabels an
  unattached cost as matched. The full-cashout estimate only moves when data
  changes (windowing, a transfer reassigned to another payout, etc.).

User-confirmed corrections live in `data/manual_matches.json` (applied before
the automatic passes; entries whose order was credited in a different cashout
are skipped, and their transfers are excluded as belonging to that payout).
When the user resolves UNMATCHED/UNATTACHED items during the session, append
entries there and rerun. Entry types:

- `"eldorado_order_id"` + `"eatransfer_uuids"` + `"reason"` — one order, its transfers
- `"eldorado_order_ids"` (list) + `"eatransfer_uuids"` — pooled same-buyer group
  (several orders share the transfers, e.g. two sales covered by three partial sends)
- `"eldorado_order_id"` + `"resolution": "no-eatransfer-cost"` — the user says
  **"Add"** for orders where the buyer never responded, so no transfer was
  needed: full credit is profit ("free money")

The user replies in shorthand: "Add" = no-eatransfer-cost; a pasted eatransfer
row (uuid, name, email, price, qty, date) = match that transfer to the named
order; "need to verify / leave" = record nothing, keep it in UNMATCHED.
Always quote the pasted uuids exactly and put the date + email evidence in
`reason`.

A blanket "everything looks fine" only ratifies matches the script already
made (check/verify tiers). It NEVER converts an UNMATCHED order into a match —
turning a candidate you suggested into a recorded match requires the user's
explicit per-item confirmation, especially for items they previously said to
leave pending.

If the script dies on an unparseable eatransfer archive row, the site likely
added a new blob layout — extend the regex chain in
`reconcile.py::parse_eatransfer_archive` (parser fixes are fine; inventing
numbers is not).

## Step 7: render the HTML report

```bash
python3 scripts/render_cashout_html.py data/cashout_YYYY-MM-DD.json data/reports/cashout-YYYY-MM-DD.html
```

All figures in the HTML come straight from the JSON.

## Step 8: hand over the local report

The report stays local — do NOT publish it as an Artifact. Open it in the
browser for the user:

```bash
open data/reports/cashout-YYYY-MM-DD.html
```

In chat, give the user the file path plus the script's INCOME / COSTS /
PROFIT / SPLIT lines verbatim and the counts of items needing review
(UNMATCHED ORDERS, in-window UNATTACHED own-tag TRANSFERS, verify-tier matches).
Point out when an unmatched order and an unattached transfer look like the
same deal (same buyer/qty/date, or a typo'd name) — but only the user may
confirm it. After each manual-match iteration, rerun steps 6–7 onto the same
file path and re-open it.

## Config

`config.json` in the project root:
- `my_tags`: eatransfer name suffixes that are this user's orders — the user
  appends their personal tag to each customer name when ordering (e.g. tag
  `ABC` makes both `Buyer_ABC` and `Buyer-ABC` theirs, any case). All script
  output and the HTML report label costs with whatever tags are configured here.
- `foreign_tags`: suffixes belonging to other people sharing the eatransfer account (excluded from profit, listed separately)
- `seller_name`: El Dorado seller account name, shown in the report header (optional)
- `quantity_tolerance_k` / `match_window_days`: matching slack (coins are often ordered a few % above the sold amount to cover EA's trade tax)
- `withdrawal`: method + fee formula used for informational fee estimates
- `net_profit_split`: optional map of name → fraction (must sum to 1) to split
  net profit between partners; omit or leave empty for no split

## Privacy rule

Never store or echo customer EA account credentials anywhere. The reconciliation
only uses usernames, emails, amounts, and order IDs.
