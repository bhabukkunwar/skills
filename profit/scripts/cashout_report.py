#!/usr/bin/env python3
"""Fully reconcile a single El Dorado cashout against eatransfer transfer costs.

Reuses reconcile.py's parsers but replaces the strict 1:1 matcher with a
multi-pass engine that handles split transfers, combined transfers, missing
tags, mistyped names and email-linked identities. Every match carries its
validation trail, and every dollar of the cashout lands in exactly one
section (the script asserts this).

Suffix policy (tags come from config.json "my_tags"; e.g. tag ABC):
  _ABC / -ABC (any case)  -> mine
  any other suffix        -> omitted, even if the base name is one of my buyers
                             (those are listed separately as 'suspicious')
  no suffix               -> candidate: linked via exact buyer name, then via
                             email seen on confirmed-mine transfers

Pass order per buyer identity (in-cashout orders only):
  P1  1:1 quantity+date (same rules as reconcile.py)
  P2  several partial transfers -> one order (unique subset sum)
  P3  one transfer -> several orders (unique subset sum)
  P4  pooled M:N when the buyer's remaining totals agree
  P4b tolerant same-buyer pairs (qty within 15%, gap <= 14d)  -> 'verify'
Then, across identities:
  P6  leftover transfers matched to orders OUTSIDE the cashout window
      (excluded from this cashout's costs, shown for completeness)
  P5  orphan transfers (own-tagged with unknown name, or unlinked) <->
      leftover orders by unique amount+date (<= 2 days)         -> 'verify'
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from decimal import Decimal
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reconcile as rc

money = rc.money
die = rc.die
D0 = Decimal("0")

TAG_RE = re.compile(r"^(.{3,}?)[-_]([A-Za-z0-9]{1,8})$")


def gap_days(a, b):
    return abs((rc.parse_date(a) - rc.parse_date(b)).total_seconds()) / 86400.0


def qty_fits(qk, qty_k, tol):
    return qty_k - tol <= qk <= qty_k * 1.05 + tol


# ---------- load ----------

def load_all():
    eo = rc.load_json("eldorado_orders_full.json")["results"]
    ep = rc.load_json("eldorado_payments_full.json")["results"]
    et_raw = rc.load_json("eatransfer_orders_full.json")
    recs, seen = [], set()
    for archived, rows in ((False, et_raw["active"]), (True, et_raw["archived"])):
        for row in rows:
            r = rc.parse_eatransfer_row(row, archived)
            if r["uuid"] not in seen:
                seen.add(r["uuid"])
                recs.append(r)
    ap = rc.RAW / "eatransfer_archive_range.json"
    if ap.exists():
        aj = json.loads(ap.read_text())
        for r in rc.parse_eatransfer_archive(aj["rows"], aj["export"]):
            if r["uuid"] not in seen:
                seen.add(r["uuid"])
                recs.append(r)
    orders = [rc.parse_eldorado_order(o) for o in eo]
    if any(o["currency"] != "USD" for o in orders):
        die("non-USD totalPrice found in El Dorado orders - script assumes USD")
    return orders, ep, recs


def cashout_window(ep, date_str):
    sale, wds = [], []
    for p in ep:
        if p["paymentState"] != "Completed":
            continue
        if p["paymentType"] == "SaleIncome":
            sale.append(p)
        elif "Withdrawal" in p["paymentType"]:
            wds.append(p)
    picks = [w for w in wds if w["createdDate"][:10] == date_str]
    if len(picks) != 1:
        die(f"expected exactly one completed withdrawal dated {date_str}, found {len(picks)}")
    co = picks[0]
    lo = max((w["createdDate"] for w in wds if w["createdDate"] < co["createdDate"]), default="")
    hi = co["createdDate"]
    win = [p for p in sale if lo < p["createdDate"] < hi]
    credited = defaultdict(Decimal)
    for p in win:
        if p["orderId"]:
            credited[p["orderId"]] += money(p["valueInUSD"]["amount"])
    win_income = sum((money(p["valueInUSD"]["amount"]) for p in win), D0)
    return co, lo, hi, credited, win_income


# ---------- classification ----------

def classify(recs, buyers_all, my_tags, cfg_foreign):
    """Attach bucket/identity/basis to each rec, in place."""
    suffix_bases = defaultdict(set)
    for r in recs:
        m = TAG_RE.match(r["name"])
        if m:
            suffix_bases[m.group(2).upper()].add(m.group(1).lower())
    auto_tags = {s for s, b in suffix_bases.items() if len(b) >= 3}
    foreign_tags = (set(cfg_foreign) | auto_tags) - my_tags
    tag_label = "/".join(sorted(my_tags))

    for r in recs:
        m = TAG_RE.match(r["name"])
        base, suf = (m.group(1), m.group(2).upper()) if m else (r["name"], None)
        if suf in my_tags:
            r.update(bucket="mine", identity=base.lower(), basis=f"{suf} tag")
        elif r["name"].lower() in buyers_all:
            r.update(bucket="candidate", identity=r["name"].lower(),
                     basis="exact buyer name, no tag")
        elif suf and base.lower() in buyers_all:
            r.update(bucket="suspicious", identity=base.lower(),
                     basis=f"my buyer's name + non-{tag_label} tag '{suf}' (typo?) - omitted per rule")
        elif suf in foreign_tags:
            r.update(bucket="foreign", identity=None, basis=f"foreign tag '{suf}'")
        else:
            r.update(bucket="unlinked", identity=None, basis="no tag, name matches no buyer")

    # email identity: emails seen on confirmed-mine transfers identify the customer
    email_map = defaultdict(set)
    for r in recs:
        if r["bucket"] in ("mine", "candidate") and r["identity"] and r["email"]:
            email_map[r["email"].lower()].add(r["identity"])
    foreign_emails = {r["email"].lower() for r in recs
                      if r["bucket"] in ("foreign", "suspicious") and r["email"]}
    for r in recs:
        if r["bucket"] != "unlinked" or not r["email"]:
            continue
        ids = email_map.get(r["email"].lower(), set())
        if len(ids) == 1:
            r.update(bucket="candidate", identity=next(iter(ids)),
                     basis=f"email {r['email']} seen on confirmed {tag_label} transfers")
        elif not ids and r["email"].lower() in foreign_emails:
            r.update(bucket="foreign", basis="email seen only on foreign-tag transfers")

    # matching quantity: requested, falling back to delivered
    for r in recs:
        r["qk"] = r["requested_k"] if r["requested_k"] > 0 else r["delivered_k"]
    return foreign_tags


# ---------- matching ----------

def rec_row(r):
    return {"uuid": r["uuid"], "name": r["name"], "email": r["email"],
            "created": r["created"], "requested_k": r["requested_k"],
            "delivered_k": r["delivered_k"], "cost": r["cost"],
            "status": r["status"], "platform": r["platform"], "basis": r["basis"]}


def order_row(o):
    return {"id": o["id"], "buyer": o["buyer"], "created": o["created"][:19].replace("T", " "),
            "qty_k": o["qty_k"], "buyer_paid": o["total_price"], "credited": o["credited"],
            "platform": o["platform"], "state": o["state"]}


def make_group(orders, grecs, confidence, pass_label, extra_notes=()):
    q_sold = sum(o["qty_k"] for o in orders)
    q_tr = sum(r["qk"] for r in grecs)
    income = sum((o["credited"] for o in orders), D0)
    cost = sum((r["cost"] for r in grecs), D0)
    checks, warnings = [], list(extra_notes)
    checks.append(f"income = exact wallet credits (${income})")
    pct = (100.0 * q_tr / q_sold) if q_sold else 0.0
    qline = f"quantity: sold {q_sold}K, transferred {q_tr}K ({pct:.1f}%)"
    if q_sold and (q_sold * 0.97 <= q_tr <= q_sold * 1.05 + 10 * len(grecs)):
        checks.append(qline)
    else:
        warnings.append(qline + " - OUTSIDE normal 97-105% band, verify")
    g = max((gap_days(o["created"], r["created"]) for o in orders for r in grecs), default=0.0)
    (checks if g <= 7 else warnings).append(f"max order-to-transfer date gap {g:.1f}d")
    plats_o = {o["platform"] for o in orders if o["platform"]}
    plats_r = {r["platform"] for r in grecs if r["platform"]}
    if plats_o and plats_r:
        (checks if plats_o == plats_r else warnings).append(
            f"platform {'/'.join(sorted(plats_o))} vs {'/'.join(sorted(plats_r))}"
            + ("" if plats_o == plats_r else " MISMATCH"))
    for r in grecs:
        if r["status"] != "finished" or r["delivered_k"] < r["requested_k"]:
            warnings.append(f"transfer {r['name']}: {r['status']}, "
                            f"{r['delivered_k']}/{r['requested_k']}K delivered")
    for o in orders:
        if o["state"] not in ("Completed", "Delivered"):
            warnings.append(f"El Dorado order {o['id'][:8]} still '{o['state']}'")
        if o.get("refunded"):
            warnings.append(f"El Dorado order {o['id'][:8]} was refunded post-completion")
    return {"buyer": orders[0]["buyer"], "orders": [order_row(o) for o in orders],
            "transfers": [rec_row(r) for r in grecs],
            "qty_sold_k": q_sold, "qty_transferred_k": q_tr,
            "income": income, "cost": cost, "profit": income - cost,
            "confidence": confidence if not warnings else
                          ("verify" if confidence != "verify" else "verify"),
            "pass": pass_label, "checks": checks, "warnings": warnings}


def best_subset(pool, lo_k, hi_k, anchor_dates, max_len=6):
    """Unique-ish subset of pool whose qk sums into [lo_k, hi_k].
    Returns (subset, ambiguous_count). Members must be within 7d of an anchor."""
    usable = [r for r in pool
              if any(gap_days(r["created"], d) <= 7 for d in anchor_dates)]
    valid = []
    for n in range(1, min(max_len, len(usable)) + 1):
        for combo in combinations(range(len(usable)), n):
            s = sum(usable[i]["qk"] for i in combo)
            if lo_k <= s <= hi_k:
                gs = max(gap_days(usable[i]["created"], d)
                         for i in combo for d in anchor_dates)
                valid.append((n, gs, s, combo))
    if not valid:
        return None, 0
    valid.sort()
    picked = [usable[i] for i in valid[0][3]]
    distinct = {frozenset(v[3]) for v in valid}
    return picked, len(distinct) - 1


def match_buyer(orders_b, recs_b, tol):
    """Run P1-P4b for one buyer identity. Returns (groups, leftover_orders, leftover_recs)."""
    groups = []
    orders_b = sorted(orders_b, key=lambda o: o["created"])
    recs_b = sorted(recs_b, key=lambda r: r["created"])
    free_o = list(orders_b)
    free_r = [r for r in recs_b if r["qk"] > 0]

    # P1: 1:1
    for r in list(free_r):
        cands = [o for o in free_o if qty_fits(r["qk"], o["qty_k"], tol)
                 and gap_days(o["created"], r["created"]) <= 7]
        if not cands:
            continue
        if len(cands) == 1:
            o, conf, notes = cands[0], "high", ()
        else:
            o = min(cands, key=lambda o: gap_days(o["created"], r["created"]))
            conf, notes = "check", (f"buyer has {len(cands)} same-size orders; paired by closest date",)
        free_o.remove(o)
        free_r.remove(r)
        groups.append(make_group([o], [r], conf, "P1 one-to-one"))
        if notes:
            groups[-1]["warnings"].extend(notes)
            groups[-1]["confidence"] = "check" if groups[-1]["confidence"] == "high" else groups[-1]["confidence"]

    # P2: several transfers -> one order
    for o in list(free_o):
        if len(free_r) < 2:
            break
        sub, ambiguous = best_subset(free_r, o["qty_k"] - tol,
                                     o["qty_k"] * 1.05 + tol, [o["created"]])
        if sub and len(sub) >= 2:
            notes = ([f"{ambiguous} alternative transfer combination(s) also sum into range"]
                     if ambiguous else [])
            for r in sub:
                free_r.remove(r)
            free_o.remove(o)
            groups.append(make_group([o], sub, "check" if ambiguous else "high",
                                     f"P2 split across {len(sub)} transfers", notes))

    # P3: one transfer -> several orders
    for r in list(free_r):
        if len(free_o) < 2:
            break
        valid = []
        for n in range(2, min(4, len(free_o)) + 1):
            for combo in combinations(range(len(free_o)), n):
                s = sum(free_o[i]["qty_k"] for i in combo)
                if s - tol <= r["qk"] <= s * 1.05 + tol and all(
                        gap_days(free_o[i]["created"], r["created"]) <= 7 for i in combo):
                    valid.append((n, s, combo))
        if valid:
            valid.sort()
            picked = [free_o[i] for i in valid[0][2]]
            ambiguous = len({frozenset(v[2]) for v in valid}) - 1
            notes = ([f"{ambiguous} alternative order combination(s) also fit"] if ambiguous else [])
            for o in picked:
                free_o.remove(o)
            free_r.remove(r)
            groups.append(make_group(picked, [r], "check" if ambiguous else "high",
                                     f"P3 one transfer covering {len(picked)} orders", notes))

    # P4: pooled M:N on totals
    if free_o and free_r:
        pool_r = [r for r in free_r if any(gap_days(r["created"], o["created"]) <= 7
                                           for o in free_o)]
        if pool_r:
            Q = sum(o["qty_k"] for o in free_o)
            T = sum(r["qk"] for r in pool_r)
            if Q - tol * max(1, len(free_o)) <= T <= Q * 1.05 + tol * len(pool_r):
                groups.append(make_group(list(free_o), pool_r, "check",
                                         f"P4 pooled {len(pool_r)} transfers over {len(free_o)} orders"))
                for r in pool_r:
                    free_r.remove(r)
                free_o.clear()

    # P4b: tolerant same-buyer pairs
    for o in list(free_o):
        best, best_gap = None, None
        for r in free_r:
            if o["qty_k"] == 0:
                continue
            ratio = r["qk"] / o["qty_k"]
            g = gap_days(o["created"], r["created"])
            if 0.85 <= ratio <= 1.15 and g <= 14 and (best is None or g < best_gap):
                best, best_gap = r, g
        if best:
            free_o.remove(o)
            free_r.remove(best)
            delta = best["qk"] - o["qty_k"]
            groups.append(make_group([o], [best], "verify", "P4b same-buyer tolerant pair",
                                     [f"quantity off by {delta:+d}K "
                                      f"({100.0 * best['qk'] / o['qty_k']:.1f}% of sold) - verify"]))
    return groups, free_o, free_r


def run(args):
    cfg = json.loads((rc.ROOT / "config.json").read_text())
    my_tags = {t.upper() for t in cfg["my_tags"]}
    tol = int(cfg["quantity_tolerance_k"])
    w_pct = Decimal(cfg["withdrawal"]["percent"])
    w_flat = Decimal(cfg["withdrawal"]["flat_usd"])

    orders, ep, recs = load_all()
    co, lo, hi, credited, win_income = cashout_window(ep, args.cashout)
    co_amt = money(co["valueInUSD"]["amount"]).copy_abs()

    win_orders = [dict(o, credited=credited[o["id"]]) for o in orders if o["id"] in credited]
    n_win_orders = len(win_orders)  # before manual overrides consume any
    missing = set(credited) - {o["id"] for o in orders}
    if missing:
        die(f"{len(missing)} credited orders missing from the orders dump - refetch El Dorado data")

    buyers_all = {o["buyer"].lower() for o in orders}
    foreign_tags = classify(recs, buyers_all, my_tags, cfg["foreign_tags"])

    zeros = [r for r in recs if r["bucket"] in ("mine", "candidate")
             and r["qk"] == 0 and (r["cost"] or D0) == D0]
    oddities = [r for r in recs if r["bucket"] in ("mine", "candidate")
                and r["qk"] == 0 and (r["cost"] or D0) > D0]
    for r in oddities:
        r["basis"] += " | ODD: cost with 0K requested/delivered"
    matchable = [r for r in recs if r["bucket"] in ("mine", "candidate")
                 and r["qk"] > 0 and r["cost"] is not None]

    # manual overrides confirmed by the user (data/manual_matches.json):
    # applied before any automatic pass; consumed orders/transfers leave the pools
    groups = []
    other_payout = []
    manual_path = rc.ROOT / "data" / "manual_matches.json"
    if manual_path.exists():
        for m in json.loads(manual_path.read_text()):
            # one order per entry ("eldorado_order_id") or a pooled group of
            # same-buyer orders sharing the transfers ("eldorado_order_ids")
            ids = m.get("eldorado_order_ids") or [m["eldorado_order_id"]]
            os_ = [x for x in win_orders if x["id"] in ids]
            if m.get("resolution") == "no-eatransfer-cost":
                if not os_:
                    continue  # belongs to a different cashout window
                for o in os_:
                    win_orders.remove(o)
                    groups.append({
                        "buyer": o["buyer"], "orders": [order_row(o)], "transfers": [],
                        "qty_sold_k": o["qty_k"], "qty_transferred_k": 0,
                        "income": o["credited"], "cost": D0, "profit": o["credited"],
                        "confidence": "high", "pass": "manual (no eatransfer cost)",
                        "checks": [f"manually confirmed: {m['reason']}",
                                   f"income = exact wallet credits (${o['credited']})"],
                        "warnings": []})
                continue
            rs = [x for x in matchable + oddities if x["uuid"] in set(m["eatransfer_uuids"])]
            if not os_:
                # orders credited in a different cashout window; any of their
                # transfers still in the pools were consumed there, not here
                oo = next((x for x in orders if x["id"] in ids), None)
                for r in rs:
                    (matchable if r in matchable else oddities).remove(r)
                    other_payout.append({**rec_row(r), "matches_order": (
                        f"{oo['buyer']} {oo['qty_k']}K sold {oo['created'][:10]}" if oo
                        else "order not in dump")
                        + " (manual match, credited outside this cashout window)"})
                continue
            if len(os_) != len(ids) or len(rs) != len(m["eatransfer_uuids"]):
                die(f"manual match for order(s) {', '.join(ids)}: all orders and all "
                    f"transfers must be present in this cashout window")
            for o in os_:
                win_orders.remove(o)
            for r in rs:
                (matchable if r in matchable else oddities).remove(r)
            g = make_group(sorted(os_, key=lambda o: o["created"]),
                           sorted(rs, key=lambda r: r["created"]), "high",
                           "manual (user-confirmed)")
            g["checks"].insert(0, f"manually confirmed: {m['reason']}")
            g["confidence"] = "high"  # user confirmation outranks mechanical warnings
            groups.append(g)

    by_buyer_o = defaultdict(list)
    for o in win_orders:
        by_buyer_o[o["buyer"].lower()].append(o)
    by_id_r = defaultdict(list)
    for r in matchable:
        by_id_r[r["identity"]].append(r)

    left_orders, left_recs = [], []
    for ident in sorted(set(by_buyer_o) | set(by_id_r)):
        g, lo_o, lo_r = match_buyer(by_buyer_o.get(ident, []), by_id_r.get(ident, []), tol)
        groups.extend(g)
        left_orders.extend(lo_o)
        left_recs.extend(lo_r)

    # P6: leftover transfers that belong to orders OUTSIDE this cashout
    out_orders = [o for o in orders if o["id"] not in credited]
    for r in list(left_recs):
        fits = [o for o in out_orders if o["buyer"].lower() == r["identity"]
                and qty_fits(r["qk"], o["qty_k"], tol)
                and gap_days(o["created"], r["created"]) <= 7]
        if fits:
            o = min(fits, key=lambda o: gap_days(o["created"], r["created"]))
            other_payout.append({**rec_row(r),
                                 "matches_order": f"{o['buyer']} {o['qty_k']}K sold {o['created'][:10]} "
                                                  f"(credited outside this cashout window)"})
            left_recs.remove(r)

    # P5: orphan amount+date across identities (unique both ways, <= 2 days)
    for r in list(left_recs):
        fits_o = [o for o in left_orders if qty_fits(r["qk"], o["qty_k"], tol)
                  and gap_days(o["created"], r["created"]) <= 2]
        if len(fits_o) != 1:
            continue
        o = fits_o[0]
        fits_r = [x for x in left_recs if qty_fits(x["qk"], o["qty_k"], tol)
                  and gap_days(o["created"], x["created"]) <= 2]
        if len(fits_r) != 1:
            continue
        left_orders.remove(o)
        left_recs.remove(r)
        groups.append(make_group([o], [r], "verify", "P5 orphan matched by amount+date",
                                 [f"eatransfer name '{r['name']}' does not contain buyer "
                                  f"'{o['buyer']}' - matched by unique amount+date, verify"]))

    # near-miss annotations for whatever is still unmatched
    others = [r for r in recs if r["bucket"] in ("suspicious", "foreign", "unlinked")
              and r.get("qk", 0) > 0] + left_recs
    unmatched_orders = []
    for o in sorted(left_orders, key=lambda x: x["created"]):
        near = [r for r in others
                if o["qty_k"] and 0.8 <= r["qk"] / o["qty_k"] <= 1.25
                and gap_days(o["created"], r["created"]) <= 4]
        near.sort(key=lambda r: gap_days(o["created"], r["created"]))
        unmatched_orders.append({**order_row(o), "near_misses": [
            f"{r['name']} [{r['bucket']}] {r['qk']}K ${r['cost']} on {r['created'][:10]}"
            for r in near[:2]]})

    # only transfers made inside the cashout window count as this cashout's
    # costs; earlier/later unattached ones belong to another payout's era
    # (they only appear once the archive dump spans several cashouts)
    win_lo = rc.parse_date(lo) if lo else None
    win_hi = rc.parse_date(hi)

    def in_window(r):
        d = rc.parse_date(r["created"])
        return (win_lo is None or d >= win_lo) and d <= win_hi

    unattached_all = sorted(left_recs + oddities, key=lambda r: r["created"])
    unattached = [r for r in unattached_all if in_window(r)]
    unattached_other = [r for r in unattached_all if not in_window(r)]

    # ---------- totals (every dollar of the cashout in exactly one bucket) ----------
    tiers = {"high": [], "check": [], "verify": []}
    for g in groups:
        tiers[g["confidence"]].append(g)
    inc = {t: sum((g["income"] for g in gs), D0) for t, gs in tiers.items()}
    cost_t = {t: sum((g["cost"] for g in gs), D0) for t, gs in tiers.items()}
    inc_unmatched = sum((o["credited"] for o in unmatched_orders), D0)
    total_income = sum(inc.values(), inc_unmatched)
    if total_income != win_income:
        die(f"income buckets sum to ${total_income} but window credits are ${win_income}")
    if win_income != co_amt:
        print(f"CHECK: window income ${win_income} != withdrawal ${co_amt}", file=sys.stderr)

    cost_matched = sum(cost_t.values(), D0)
    cost_unattached = sum((r["cost"] for r in unattached), D0)
    fee = (co_amt * w_pct + w_flat).quantize(Decimal("0.01"))
    net = win_income - cost_matched - cost_unattached - fee

    # net profit split (config: net_profit_split); rounding remainder goes to the largest share
    split = {}
    if cfg.get("net_profit_split"):
        shares = {n: Decimal(p) for n, p in cfg["net_profit_split"].items()}
        if sum(shares.values()) != 1:
            die(f"net_profit_split percentages sum to {sum(shares.values())}, expected 1")
        split = {n: (net * p).quantize(Decimal("0.01")) for n, p in shares.items()}
        largest = max(split, key=lambda n: split[n])
        split[largest] += net - sum(split.values(), D0)

    report = {
        "cashout": {"date": args.cashout, "type": co["paymentType"], "amount": co_amt,
                    "window_start": (lo or "account start")[:19].replace("T", " "),
                    "window_end": hi[:19].replace("T", " "),
                    "orders_in_window": n_win_orders, "window_income": win_income},
        "config": {"my_tags": sorted(my_tags), "detected_foreign_tags": sorted(foreign_tags),
                   "quantity_tolerance_k": tol, "match_window_days": cfg["match_window_days"],
                   "seller_name": cfg.get("seller_name", ""),
                   "withdrawal_method": cfg["withdrawal"]["method"],
                   "withdrawal_percent": str(w_pct), "withdrawal_flat_usd": str(w_flat)},
        "groups": sorted(groups, key=lambda g: g["orders"][0]["created"]),
        "summary": {
            "matched_orders": sum(len(g["orders"]) for g in groups),
            "matched_transfers": sum(len(g["transfers"]) for g in groups),
            "income_high": inc["high"], "income_check": inc["check"],
            "income_verify": inc["verify"], "income_unmatched": inc_unmatched,
            "cost_high": cost_t["high"], "cost_check": cost_t["check"],
            "cost_verify": cost_t["verify"], "cost_matched": cost_matched,
            "cost_unattached": cost_unattached,
            "profit_matched": sum(inc.values(), D0) - cost_matched,
            "profit_full_estimate": win_income - cost_matched - cost_unattached,
            "withdrawal_fee_est": fee,
            "net_after_fee_est": net,
            "net_split": {n: str(v) for n, v in split.items()},
            "net_split_pct": dict(cfg.get("net_profit_split", {})),
        },
        "unmatched_orders": unmatched_orders,
        "unattached_transfers": [rec_row(r) for r in unattached],
        "unattached_other_windows_transfers": [rec_row(r) for r in unattached_other],
        "other_payout_transfers": sorted(other_payout, key=lambda r: r["created"]),
        "suspicious_transfers": [rec_row(r) for r in recs if r["bucket"] == "suspicious"],
        "unlinked_transfers": [rec_row(r) for r in recs
                               if r["bucket"] == "unlinked" and r.get("qk", 0) > 0],
        "zero_transfers_ignored": len(zeros),
        "foreign_total_cost": sum((r["cost"] or D0 for r in recs if r["bucket"] == "foreign"), D0),
        "foreign_count": sum(1 for r in recs if r["bucket"] == "foreign"),
    }
    return report


def print_text(rep):
    s = rep["summary"]
    c = rep["cashout"]
    tag = "/".join(rep["config"]["my_tags"])
    print("=" * 66)
    print(f"CASHOUT RECONCILIATION  {c['date']}  {c['type']}  ${c['amount']}")
    print(f"window {c['window_start']} -> {c['window_end']} (UTC), "
          f"{c['orders_in_window']} orders, income ${c['window_income']}")
    print("=" * 66)
    for g in rep["groups"]:
        o0 = g["orders"][0]
        print(f"\n[{g['confidence'].upper():6}] {g['buyer']}  "
              f"sold {g['qty_sold_k']}K / transferred {g['qty_transferred_k']}K  "
              f"income ${g['income']}  cost ${g['cost']}  profit ${g['profit']}  ({g['pass']})")
        for line in g["warnings"]:
            print(f"    !! {line}")
    print(f"\nINCOME:  high ${s['income_high']} + check ${s['income_check']} + "
          f"verify ${s['income_verify']} + unmatched ${s['income_unmatched']} "
          f"= ${c['window_income']}")
    print(f"COSTS:   matched ${s['cost_matched']} + unattached {tag} ${s['cost_unattached']}")
    print(f"PROFIT:  matched ${s['profit_matched']}  |  full-cashout estimate "
          f"${s['profit_full_estimate']}  |  fee ~${s['withdrawal_fee_est']}  "
          f"|  net est ${s['net_after_fee_est']}")
    if s["net_split"]:
        parts = "  |  ".join(f"{n} ({Decimal(s['net_split_pct'][n]):.0%}) ${v}"
                             for n, v in s["net_split"].items())
        print(f"SPLIT:   {parts}")
    if rep["unmatched_orders"]:
        print(f"\nUNMATCHED ORDERS ({len(rep['unmatched_orders'])}) - income counted, no cost found:")
        for o in rep["unmatched_orders"]:
            print(f"  {o['created'][:10]}  {o['buyer']:<22} {o['qty_k']:>6}K  credited ${o['credited']}")
            for nm in o["near_misses"]:
                print(f"      near miss: {nm}")
    if rep["unattached_transfers"]:
        print(f"\nUNATTACHED {tag} TRANSFERS ({len(rep['unattached_transfers'])}) - cost counted, no order:")
        for r in rep["unattached_transfers"]:
            print(f"  {r['created'][:10]}  {r['name']:<26} {r['requested_k']:>6}K  ${r['cost']}  [{r['basis']}]")
    if rep["unattached_other_windows_transfers"]:
        print(f"\nUNATTACHED {tag} TRANSFERS OUTSIDE WINDOW ({len(rep['unattached_other_windows_transfers'])}) "
              f"- another payout's era, not counted:")
        for r in rep["unattached_other_windows_transfers"]:
            print(f"  {r['created'][:10]}  {r['name']:<26} {r['requested_k']:>6}K  ${r['cost']}  [{r['basis']}]")
    if rep["other_payout_transfers"]:
        print(f"\nBELONGS TO ANOTHER PAYOUT ({len(rep['other_payout_transfers'])}) - excluded:")
        for r in rep["other_payout_transfers"]:
            print(f"  {r['created'][:10]}  {r['name']:<26} {r['requested_k']:>6}K  ${r['cost']}  -> {r['matches_order']}")
    if rep["suspicious_transfers"]:
        print(f"\nSUSPICIOUS (my buyer's name + non-{tag} tag, omitted) ({len(rep['suspicious_transfers'])}):")
        for r in rep["suspicious_transfers"]:
            print(f"  {r['created'][:10]}  {r['name']:<26} {r['requested_k']:>6}K  ${r['cost']}")
    print(f"\nforeign-tag transfers omitted: {rep['foreign_count']} (${rep['foreign_total_cost']}); "
          f"unlinked kept aside: {len(rep['unlinked_transfers'])}; "
          f"zero-value rows ignored: {rep['zero_transfers_ignored']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cashout", required=True, help="withdrawal date YYYY-MM-DD")
    ap.add_argument("--json", help="write full report JSON to this path")
    args = ap.parse_args()
    rep = run(args)
    print_text(rep)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=1, default=str))
        print(f"\nJSON written to {args.json}")


if __name__ == "__main__":
    main()
