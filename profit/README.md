# /profit — El Dorado ↔ eatransfer profit reconciliation

A [Claude Code](https://claude.com/claude-code) skill for people who resell
EAFC coins: sell on [eldorado.gg](https://www.eldorado.gg), fulfill by buying
transfers on [eatransfer.top](https://eatransfer.top). Type `/profit` and it
fetches fresh data from both platforms (through your own logged-in browser via
Playwright MCP) and reconciles every dollar of an El Dorado cashout against
its eatransfer transfer costs — matched with a validation trail, never guessed.

The deliverable is a per-cashout HTML report: income tiers, matched groups
(1:1, split, combined, pooled), unmatched orders with nearest candidates,
unattached costs, excluded transfers with reasons, and an optional partner
profit split.

## How your orders are identified — the tag

When you order a transfer on eatransfer, append **your personal tag** to the
customer name: `SomeBuyer_ABC` or `SomeBuyer-ABC`. The scripts treat any name
ending in `_<tag>` / `-<tag>` (any case) as yours — set your tag(s) in
`config.json`:

```json
"my_tags": ["ABC"]
```

Everything downstream follows this setting: matching, cost buckets, and every
label in the text and HTML reports. If several people share one eatransfer
account, each uses their own tag; list your partners' tags in `foreign_tags`
so their transfers are excluded from your numbers (and listed separately).
Untagged transfers are still linkable through the exact buyer name or a
customer email already seen on your confirmed transfers — but always flagged
for you to verify.

## Requirements

- Claude Code with the Playwright MCP server:
  `claude mcp add playwright -- npx @playwright/mcp@latest`
- Python 3.9+ (standard library only)
- Seller account on eldorado.gg, fulfillment account on eatransfer.top
  (you log in yourself in the Playwright browser window — the skill never
  handles credentials)

## Install

From this folder, into a new (or existing) project directory:

```bash
mkdir -p myproject/.claude/skills/profit myproject/data/raw myproject/data/reports
cp -r scripts myproject/
cp config.example.json myproject/config.json
cp SKILL.md myproject/.claude/skills/profit/SKILL.md
```

Edit `myproject/config.json` — at minimum set `my_tags` to your suffix.
Then open Claude Code inside `myproject` and type `/profit`.

> Keep `data/` out of version control — the raw dumps contain your customers'
> emails and order history.

## Config reference

| Key | Meaning |
|---|---|
| `my_tags` | Your eatransfer name suffix(es). `["ABC"]` claims `Buyer_ABC` and `Buyer-ABC`, any case. |
| `foreign_tags` | Tags of other people sharing the eatransfer account — excluded from your numbers. |
| `seller_name` | Your El Dorado seller name, shown in the report header (optional). |
| `quantity_tolerance_k` | Matching slack in K coins (transfers are usually ordered a few % above the sold amount to cover EA's trade tax). |
| `match_window_days` | Max days between an El Dorado sale and its eatransfer transfer for automatic matching. |
| `eldorado_commission_rate` | Used only to estimate income for orders not yet credited (El Dorado credits `price × (1 − rate)`). |
| `withdrawal` | Fee formula of your withdrawal method (`percent` of amount + `flat_usd`), for the net-after-fee estimate. |
| `net_profit_split` | Optional `{"Name": "0.40", ...}` (fractions sum to 1) — splits net profit between partners as KPI tiles. |

## What's in here

| File | Role |
|---|---|
| `SKILL.md` | The skill: fetch steps (El Dorado orders + wallet payments, eatransfer active/archived/deep-archive orders), then run the scripts. |
| `scripts/reconcile.py` | Strict 1:1 matcher + overview report. All money math in `Decimal`; anything ambiguous is flagged, never silently matched. |
| `scripts/cashout_report.py` | Per-cashout multi-pass matcher (1:1, split, combined, pooled, tolerant, orphan) with per-group validation trails; asserts income buckets sum exactly to the withdrawal. |
| `scripts/render_cashout_html.py` | Renders the JSON report as a self-contained HTML page (light/dark). |

User-confirmed corrections go in `data/manual_matches.json` (see SKILL.md
step 6); they are applied before the automatic passes on every rerun.

## Caveats

- Built against the platforms' internal JSON endpoints as observed 2026-07/08.
  The scripts fail loudly (non-zero exit, no invented numbers) if a response
  shape changes.
- Assumes USD prices on El Dorado.
- Split transfers, typo'd tags, and cross-platform deliveries are handled by
  `cashout_report.py`'s passes but land in `check`/`verify` tiers — the report
  always shows you the evidence and leaves confirmation to you.
