#!/usr/bin/env python3
"""Reconcile El Dorado coin sales against eatransfer purchase costs.

Reads raw JSON dumps produced by the /profit skill fetch step:
  data/raw/eldorado_orders_full.json   - seller orders (api/orders/me/seller/orders)
  data/raw/eldorado_payments_full.json - wallet payments (api/userpayment/me/payments)
  data/raw/eatransfer_orders_full.json - {active, archived} rows from getOrders.php

All money math uses Decimal. Anything ambiguous is flagged for review,
never silently matched. Exit code 1 on any structural problem with the data.
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data"

EATRANSFER_ROW_LEN = 15  # observed 2026-07: fail loudly if the site changes shape
ARCHIVE_ROW_LEN = 20     # getOrdersArchive.php rows (orderArchive.php table), observed 2026-07


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_json(name):
    path = RAW / name
    if not path.exists():
        die(f"missing {path} - run the fetch step of the /profit skill first")
    with open(path) as f:
        return json.load(f)


def strip_tags(html):
    return re.sub(r"<[^>]+>", "", html)


def money(x):
    # go through str() so float artifacts like 5.159999 never enter Decimal
    return Decimal(str(x)).quantize(Decimal("0.01"))


# ---------- eatransfer ----------

def parse_eatransfer_row(row, archived):
    if not isinstance(row, list) or len(row) != EATRANSFER_ROW_LEN:
        die(f"eatransfer row has unexpected shape ({len(row)} cols, expected "
            f"{EATRANSFER_ROW_LEN}) - site layout may have changed: {str(row)[:200]}")
    uuid, blob = row[0], str(row[1])
    name = strip_tags(str(row[7]).split("<br>")[0]).strip()
    if not name:
        die(f"could not extract customer name for eatransfer order {uuid}")
    m = re.search(r"(\d+(?:\.\d+)?)\s*\$\s*\|\s*Sort:", blob)
    cost = money(m.group(1)) if m else None  # failed orders can have no price
    cm = re.search(r"([\d,]+)\s*/\s*([\d,]+)\s*K", str(row[2]))
    if not cm:
        die(f"could not extract coin amounts for eatransfer order {uuid}: {row[2]!r}")
    delivered_k = int(cm.group(1).replace(",", ""))
    requested_k = int(cm.group(2).replace(",", ""))
    return {
        "uuid": uuid,
        "name": name,
        "cost": cost,
        "delivered_k": delivered_k,
        "requested_k": requested_k,
        "created": str(row[3]),
        "status": str(row[10]).strip(),
        "email": str(row[11]).strip(),
        "platform": str(row[13]).strip(),  # PS / XB / PC
        "archived": archived,
    }


def parse_eatransfer_archive(table_rows, export_rows):
    """Merge getOrdersArchive.php table rows (20 cols, has coins/status/cost)
    with getOrdersArchiveExport.php rows (15 cols flat, has clean 'name - email')
    into the same record shape parse_eatransfer_row produces."""
    exports = {}
    for row in export_rows:
        if not isinstance(row, list) or len(row) != EATRANSFER_ROW_LEN:
            die(f"archive export row has unexpected shape ({len(row)} cols, expected "
                f"{EATRANSFER_ROW_LEN}): {str(row)[:200]}")
        exports[row[0]] = row
    recs = []
    for row in table_rows:
        if not isinstance(row, list) or len(row) != ARCHIVE_ROW_LEN:
            die(f"archive table row has unexpected shape ({len(row)} cols, expected "
                f"{ARCHIVE_ROW_LEN}): {str(row)[:200]}")
        uuid = row[0]
        ex = exports.get(uuid)
        if ex is None:
            die(f"archive order {uuid} present in table dump but missing from export dump")
        blob = str(row[1])
        # drop the risk-level badge so its number doesn't glue onto the name, then
        # the visible text is 'NAME - email - PRICE $' (email sometimes absent)
        text = strip_tags(re.sub(r"<span[^>]*badge[^>]*>.*?</span>", "", blob)).strip()
        # API-created orders render as 'API PRICE $ - NAME - email' instead
        # (newer ones as 'API <uuid> - PRICE $ - NAME - email');
        # a few older rows carry no price and end in '| Sort: .. | Limit: ..'
        m = (re.match(r"^(?P<name>.+?)\s+-\s+(?P<email>\S+@\S+)\s+-\s+(?P<price>\d+(?:\.\d+)?)\s*\$$", text)
             or re.match(r"^(?P<name>.+?)\s+-\s+(?P<price>\d+(?:\.\d+)?)\s*\$$", text)
             or re.match(r"^API\s+(?P<price>\d+(?:\.\d+)?)\s*\$\s+-\s+(?P<name>.+?)\s+-\s+(?P<email>\S+@\S+)$", text)
             or re.match(r"^API\s+(?P<price>\d+(?:\.\d+)?)\s*\$\s+-\s+(?P<name>.+?)$", text)
             or re.match(r"^API\s+[0-9a-fA-F-]{30,40}\s+-\s+(?P<price>\d+(?:\.\d+)?)\s*\$\s+-\s+(?P<name>.+?)(?:\s+-\s+(?P<email>\S+@\S+))?$", text)
             or re.match(r"^(?P<name>.+?)\s+-\s+(?P<email>\S+@\S+)\s*\|\s*Sort:", text))
        if not m:
            die(f"could not parse archive order {uuid} row text: {text!r}")
        name = m.group("name").strip()
        email = (m.groupdict().get("email") or "").strip()
        if m.groupdict().get("price") is not None:
            cost = money(m.group("price"))
            if money(row[18]) != cost and money(ex[6]) != cost:
                die(f"archive order {uuid}: blob price {cost} matches neither cost "
                    f"column {row[18]} nor export price {ex[6]}")
        else:
            cost = money(row[18])
            if money(ex[6]) != cost:
                die(f"archive order {uuid}: cost column {row[18]} != export price {ex[6]}")
        # export names are 'API' for API-created orders; cross-check the rest
        ex_name = str(ex[1]).strip().rpartition(" - ")[0].strip() or str(ex[1]).strip()
        if ex_name != "API" and ex_name != name:
            die(f"archive order {uuid}: export name {ex_name!r} != table name {name!r}")
        cm = re.search(r"([\d,]+)\s*/\s*([\d,]+)\s*K", str(row[2]))
        if not cm:
            die(f"could not extract coin amounts for archive order {uuid}: {row[2]!r}")
        recs.append({
            "uuid": uuid,
            "name": name,
            "cost": cost,
            "delivered_k": int(cm.group(1).replace(",", "")),
            "requested_k": int(cm.group(2).replace(",", "")),
            "created": str(row[3]),
            "status": strip_tags(str(row[6])).strip().lower(),
            "email": email,
            "platform": str(row[12]).strip(),
            "archived": True,
        })
    return recs


def split_tag(name, known_tags):
    """'SomeBuyer-Xy1_TAG' / '-TAG' -> ('SomeBuyer-Xy1', 'TAG') for any TAG in known_tags
    (config.json my_tags/foreign_tags); tag None if no known suffix."""
    m = re.match(r"^(.*)[-_]([A-Za-z0-9]+)$", name)
    if m and m.group(2).upper() in known_tags:
        return m.group(1), m.group(2).upper()
    return name, None


# ---------- El Dorado ----------

ELDORADO_DEVICE_TO_ET = {"PlayStation": "PS", "Xbox": "XB", "PC": "PC"}


def parse_eldorado_order(o):
    device = ""
    for prop in (o.get("orderOfferDetails") or {}).get("tradeEnvironmentProperties") or []:
        if prop.get("name") == "Device":
            device = ELDORADO_DEVICE_TO_ET.get(prop.get("value", ""), prop.get("value", ""))
    return {
        "id": o["id"],
        "buyer": o["buyerUsername"],
        "qty_k": int(o["purchaseQuantity"]),
        "total_price": money(o["totalPrice"]["amount"]),
        "currency": o["totalPrice"]["currency"],
        "state": o["state"]["state"],
        "created": o["createdDate"],
        "platform": device,
        "refunded": bool(o.get("hasBeenRefundedPostCompletion")),
        "canceled": o.get("cancelation") is not None,
    }


def parse_date(s):
    s = s.replace("Z", "").split(".")[0].replace("T", " ")
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="only consider El Dorado orders created on/after this date (YYYY-MM-DD)")
    ap.add_argument("--cashout", help="only consider El Dorado orders whose sale income was credited "
                                      "between the previous withdrawal and the withdrawal on this date (YYYY-MM-DD)")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text())
    my_tags = {t.upper() for t in cfg["my_tags"]}
    foreign_tags = {t.upper() for t in cfg["foreign_tags"]}
    tol_k = int(cfg["quantity_tolerance_k"])
    window = timedelta(days=int(cfg["match_window_days"]))
    commission = Decimal(cfg["eldorado_commission_rate"])
    w_pct = Decimal(cfg["withdrawal"]["percent"])
    w_flat = Decimal(cfg["withdrawal"]["flat_usd"])

    # --- load ---
    eo = load_json("eldorado_orders_full.json")["results"]
    ep = load_json("eldorado_payments_full.json")["results"]
    et_raw = load_json("eatransfer_orders_full.json")

    et_orders, seen = [], set()
    for archived, rows in ((False, et_raw["active"]), (True, et_raw["archived"])):
        for row in rows:
            rec = parse_eatransfer_row(row, archived)
            if rec["uuid"] not in seen:
                seen.add(rec["uuid"])
                et_orders.append(rec)

    archive_note = ""
    archive_path = RAW / "eatransfer_archive_range.json"
    if archive_path.exists():
        aj = json.loads(archive_path.read_text())
        added = 0
        for rec in parse_eatransfer_archive(aj["rows"], aj["export"]):
            if rec["uuid"] not in seen:
                seen.add(rec["uuid"])
                et_orders.append(rec)
                added += 1
        arch_dates = sorted(r[3] for r in aj["rows"])
        archive_note = (f"archive range dump: {added} extra orders loaded "
                        f"({arch_dates[0][:10]} .. {arch_dates[-1][:10]})" if aj["rows"]
                        else "archive range dump present but empty")

    orders = [parse_eldorado_order(o) for o in eo]
    if any(o["currency"] != "USD" for o in orders):
        die("non-USD totalPrice found in El Dorado orders - script assumes USD")
    if args.since:
        orders = [o for o in orders if o["created"][:10] >= args.since]

    # exact credited income per order (net of commission) from the wallet feed
    sale_income, withdrawals = [], []
    for p in ep:
        if p["paymentState"] != "Completed":
            continue
        if p["paymentType"] == "SaleIncome":
            sale_income.append(p)
        elif "Withdrawal" in p["paymentType"]:
            withdrawals.append(p)

    cashout = None
    if args.cashout:
        picks = [w for w in withdrawals if w["createdDate"][:10] == args.cashout]
        if len(picks) != 1:
            die(f"expected exactly one completed withdrawal dated {args.cashout}, "
                f"found {len(picks)} (on record: {[w['createdDate'][:10] for w in withdrawals]})")
        cashout = picks[0]
        prev = [w["createdDate"] for w in withdrawals if w["createdDate"] < cashout["createdDate"]]
        lo, hi = (max(prev) if prev else ""), cashout["createdDate"]
        sale_income = [p for p in sale_income if lo < p["createdDate"] < hi]
        in_cashout = {p["orderId"] for p in sale_income if p["orderId"]}
        orders = [o for o in orders if o["id"] in in_cashout]

    credited = defaultdict(Decimal)
    for p in sale_income:
        if p["orderId"]:
            credited[p["orderId"]] += money(p["valueInUSD"]["amount"])

    # --- bucket eatransfer orders ---
    mine, foreign, untagged, failed = [], [], [], []
    for rec in et_orders:
        base, tag = split_tag(rec["name"], my_tags | foreign_tags)
        rec["base"] = base
        rec["tag"] = tag
        if rec["cost"] is None:
            if rec["delivered_k"] == 0:
                failed.append(rec)
            else:
                die(f"eatransfer order {rec['uuid']} ({rec['name']}) delivered "
                    f"{rec['delivered_k']}K but has no cost - needs manual look")
            continue
        (mine if tag in my_tags else foreign if tag in foreign_tags else untagged).append(rec)

    et_window_start = min((r["created"] for r in et_orders), default=None)

    # --- El Dorado orders relevant for matching ---
    sellable, in_progress, excluded = [], [], []
    for o in orders:
        if o["canceled"] or o["state"] == "Canceled":
            excluded.append(o)
        elif o["state"] in ("Completed", "Delivered"):
            (excluded if o["refunded"] else sellable).append(o)
        else:  # Initialized / Paid etc.
            in_progress.append(o)

    # transfers usually run while the El Dorado order is still open,
    # so Paid/Delivered orders must be matchable too
    by_buyer = defaultdict(list)
    for o in sellable + in_progress:
        by_buyer[o["buyer"].lower()].append(o)

    # --- match ---
    matched, review, unmatched_et = [], [], []
    used_eldorado = set()

    for rec in sorted(mine, key=lambda r: r["created"]):
        cands = [o for o in by_buyer.get(rec["base"].lower(), []) if o["id"] not in used_eldorado]
        # transfers are often ordered a few % above the sold amount to cover EA's trade tax
        qty_ok = [o for o in cands
                  if o["qty_k"] - tol_k <= rec["requested_k"] <= o["qty_k"] * 1.05 + tol_k]
        in_window = [o for o in qty_ok
                     if abs(parse_date(o["created"]) - parse_date(rec["created"])) <= window]
        note = ""
        if len(in_window) == 1:
            pick = in_window[0]
        elif len(in_window) > 1:
            # several same-size orders for the same buyer: pair by closest date, flag it
            pick = min(in_window, key=lambda o: abs(parse_date(o["created"]) - parse_date(rec["created"])))
            note = f"buyer has {len(in_window)} same-size orders; paired by closest date - verify"
        elif qty_ok:
            review.append({**rec, "reason": (
                f"El Dorado buyer '{rec['base']}' has a same-size order but more than "
                f"{window.days} days away from this transfer - match it yourself if it's right")})
            continue
        elif cands:
            review.append({**rec, "reason": (
                f"El Dorado buyer '{rec['base']}' found but no order with a compatible "
                f"amount for {rec['requested_k']}K (has: {[o['qty_k'] for o in cands]}K)")})
            continue
        else:
            unmatched_et.append(rec)
            continue

        used_eldorado.add(pick["id"])
        if pick["state"] not in ("Completed", "Delivered"):
            note = (note + "; " if note else "") + f"El Dorado order still '{pick['state']}' - not final"
        income = credited.get(pick["id"])
        estimated = income is None
        if estimated:
            income = money(pick["total_price"] * (1 - commission))
            note = (note + "; " if note else "") + "no wallet record yet - income estimated as price minus 5%"
        if rec["status"] != "finished" or rec["delivered_k"] < rec["requested_k"]:
            note = (note + "; " if note else "") + (
                f"transfer '{rec['status']}' but only {rec['delivered_k']}/{rec['requested_k']}K "
                f"delivered - cost may grow as the rest is sent")
        matched.append({
            "eldorado_id": pick["id"], "eatransfer_id": rec["uuid"],
            "buyer": pick["buyer"], "qty_k": pick["qty_k"],
            "buyer_paid": pick["total_price"], "received": income,
            "transfer_cost": rec["cost"], "profit": income - rec["cost"],
            "sold_date": pick["created"][:10], "platform_match": pick["platform"] == rec["platform"],
            "estimated_income": estimated, "needs_check": bool(note), "note": note,
        })

    # El Dorado sales inside the eatransfer window with no transfer yet
    unmatched_eldorado = []
    if et_window_start:
        for o in sellable:
            if o["id"] not in used_eldorado and o["created"][:19].replace("T", " ") >= et_window_start:
                unmatched_eldorado.append(o)

    # --- write CSVs ---
    OUT.mkdir(exist_ok=True)
    if matched:
        with open(OUT / "matched.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(matched[0].keys()))
            w.writeheader()
            w.writerows(matched)

    # --- report ---
    total_received = sum((m["received"] for m in matched), Decimal(0))
    total_cost = sum((m["transfer_cost"] for m in matched), Decimal(0))
    total_profit = total_received - total_cost

    print("=" * 62)
    print("PROFIT RECONCILIATION - El Dorado vs eatransfer")
    print(f"eatransfer history starts {et_window_start or 'n/a'};"
          f" El Dorado orders considered: {len(orders)}")
    if archive_note:
        print(archive_note)
    if cashout:
        amt = money(cashout["valueInUSD"]["amount"]).copy_abs()
        win_income = sum((money(p["valueInUSD"]["amount"]) for p in sale_income), Decimal(0))
        prev_ts = max((w["createdDate"] for w in withdrawals if w["createdDate"] < cashout["createdDate"]),
                      default="account start")
        print(f"CASHOUT {args.cashout}: {cashout['paymentType']} ${amt}")
        print(f"  income window: {prev_ts[:19].replace('T', ' ')} -> {cashout['createdDate'][:19].replace('T', ' ')} (UTC)")
        print(f"  sale income credited in window: ${win_income} across {len(orders)} orders")
        if win_income != amt:
            print(f"  << CHECK: window income ${win_income} does not equal withdrawal ${amt} - "
                  f"leftover balance or other wallet movements in play")
    print("=" * 62)
    print(f"\nMATCHED ORDERS: {len(matched)}")
    for m in matched:
        flag = "  << CHECK: " + m["note"] if m["needs_check"] else ""
        plat = "" if m["platform_match"] else "  [platform mismatch!]"
        print(f"  {m['sold_date']}  {m['buyer']:<22} {m['qty_k']:>6}K  "
              f"recv ${m['received']:>7}  cost ${m['transfer_cost']:>6}  "
              f"profit ${m['profit']:>7}{plat}{flag}")
    print(f"\n  Total received : ${total_received}")
    print(f"  Total cost     : ${total_cost}")
    print(f"  GROSS PROFIT   : ${total_profit}")

    print(f"\n  Reminder: withdrawing via {cfg['withdrawal']['method']} costs "
          f"{w_pct:.0%} + ${w_flat} of the amount withdrawn (your whole revenue,")
    print("  not just profit). Estimated fees per recorded withdrawal are listed below.")

    if unmatched_eldorado:
        print(f"\nEL DORADO SALES WITH NO MATCHING TRANSFER ({len(unmatched_eldorado)}) - money in, coins not sent via eatransfer?")
        for o in sorted(unmatched_eldorado, key=lambda x: x["created"]):
            print(f"  {o['created'][:10]}  {o['buyer']:<22} {o['qty_k']:>6}K  paid ${o['total_price']}  [{o['state']}]")
    if unmatched_et:
        print(f"\nTRANSFERS WITH NO MATCHING EL DORADO SALE ({len(unmatched_et)}) - cost incurred, no sale found:")
        for r in sorted(unmatched_et, key=lambda x: x["created"]):
            print(f"  {r['created'][:10]}  {r['name']:<26} {r['requested_k']:>6}K  cost ${r['cost']}  [{r['status']}]")
    if review:
        print(f"\nNEEDS MANUAL REVIEW ({len(review)}):")
        for r in review:
            print(f"  {r['created'][:10]}  {r['name']:<26} {r['requested_k']:>6}K  cost ${r['cost']}")
            print(f"      -> {r['reason']}")
    in_progress_unused = [o for o in in_progress if o["id"] not in used_eldorado]
    if in_progress_unused:
        print(f"\nEL DORADO ORDERS IN PROGRESS, NO TRANSFER YET ({len(in_progress_unused)}):")
        for o in in_progress_unused:
            print(f"  {o['created'][:10]}  {o['buyer']:<22} {o['qty_k']:>6}K  ${o['total_price']}  [{o['state']}]")
    if foreign:
        f_cost = sum((r["cost"] for r in foreign), Decimal(0))
        print(f"\nFOREIGN-TAG TRANSFERS EXCLUDED ({len(foreign)}, tags {sorted(foreign_tags)}): total cost ${f_cost}")
        for r in sorted(foreign, key=lambda x: x["created"]):
            print(f"  {r['created'][:10]}  {r['name']:<26} {r['requested_k']:>6}K  cost ${r['cost']}  [{r['status']}]")
    if failed:
        print(f"\nFAILED TRANSFERS, NOTHING DELIVERED ({len(failed)}) - no cost recorded, not counted:")
        for r in failed:
            print(f"  {r['created'][:10]}  {r['name']:<26} 0/{r['requested_k']}K  [{r['status']}]")
    if untagged:
        print(f"\nUNTAGGED TRANSFERS ({len(untagged)}) - no known suffix, not counted:")
        for r in untagged:
            print(f"  {r['created'][:10]}  {r['name']:<26} {r['requested_k']:>6}K  cost ${r['cost']}")
    if withdrawals:
        print(f"\nWITHDRAWALS ON RECORD ({len(withdrawals)}):")
        for p in sorted(withdrawals, key=lambda x: x["createdDate"]):
            amt = money(p["valueInUSD"]["amount"]).copy_abs()
            fee = (amt * w_pct + w_flat).quantize(Decimal("0.01")) if "USDC" in p["paymentType"] else None
            fee_note = f"  (est. fee ~ ${fee})" if fee is not None else ""
            print(f"  {p['createdDate'][:10]}  {p['paymentType']:<18} ${amt}{fee_note}")

    print(f"\nCSV written to {OUT / 'matched.csv'}" if matched else "\nNo matches - no CSV written")


if __name__ == "__main__":
    main()
