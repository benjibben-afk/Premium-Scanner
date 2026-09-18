#!/usr/bin/env python3
"""
Short-premium scanner for sub-$10 stocks.

Finds cash-secured puts and covered calls that pay 3-10% ROI in 3-10 days
(plus a second tier at 11-21 days), grades each setup A/B/C, writes a
navy/gold HTML dashboard + JSON, and emails new A-grade hits.

Data:  Yahoo screener (universe, earnings dates, price history)
       CBOE delayed quotes (option chains, Greeks, OI, bid/ask, IV) ~15 min delay
Run:   python scanner.py            (full run)
       SCAN_LIMIT=60 python scanner.py   (quick test on 60 tickers)
"""
import csv
import datetime as dt
import json
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import numpy as np
import requests
import yfinance as yf
from yfinance import EquityQuery as Q

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text())
STATE = ROOT / CFG["output"]["state_dir"]
STATE.mkdir(exist_ok=True)
(ROOT / "docs").mkdir(exist_ok=True)

ET = dt.timezone(dt.timedelta(hours=-4))  # good enough for timestamps; DST drift is cosmetic
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.astimezone(ET).date()
OPT_RE = re.compile(r"^([A-Z.]+?)(\d{6})([CP])(\d{8})$")

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0 (cc-scanner)"


def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# 1. Universe
# ----------------------------------------------------------------------------
def load_watchlist():
    p = ROOT / CFG["universe"]["always_include"]
    if not p.exists():
        return []
    return [l.strip().upper() for l in p.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def build_universe():
    u = CFG["universe"]
    q = Q("and", [
        Q("btwn", ["intradayprice", u["price_min"], u["price_max"]]),
        Q("gt", ["avgdailyvol3m", u["min_avg_volume"]]),
        Q("eq", ["region", "us"]),
        Q("is-in", ["exchange"] + u["exchanges"]),
    ])
    rows, off = [], 0
    while True:
        try:
            r = yf.screen(q, offset=off, size=250, sortField="avgdailyvol3m", sortAsc=False)
        except Exception as e:
            log(f"screener error at offset {off}: {e}")
            break
        qs = r.get("quotes", [])
        rows += qs
        if len(qs) < 250:
            break
        off += 250
    out = {}
    for x in rows:
        if x.get("quoteType") != "EQUITY":
            continue
        out[x["symbol"]] = dict(
            sym=x["symbol"], name=x.get("shortName") or x.get("longName") or x["symbol"],
            price=x.get("regularMarketPrice"), chg=x.get("regularMarketChangePercent"),
            vol=x.get("averageDailyVolume3Month"), mcap=x.get("marketCap"),
            earn=x.get("earningsTimestamp"), earnS=x.get("earningsTimestampStart"),
            hi52=x.get("fiftyTwoWeekHigh"), lo52=x.get("fiftyTwoWeekLow"),
        )
    # watchlist names the screener may have skipped
    missing = [s for s in load_watchlist() if s not in out]
    if missing:
        try:
            for s in missing:
                fi = yf.Ticker(s).fast_info
                px = fi.get("last_price")
                if px:
                    out[s] = dict(sym=s, name=s, price=px, chg=None, vol=fi.get("three_month_average_volume"),
                                  mcap=fi.get("market_cap"), earn=None, earnS=None,
                                  hi52=fi.get("year_high"), lo52=fi.get("year_low"), watch=True)
        except Exception as e:
            log(f"watchlist fetch problem: {e}")
    log(f"universe: {len(out)} names")
    return out


# ----------------------------------------------------------------------------
# 2. Option chains (CBOE)
# ----------------------------------------------------------------------------
def load_no_chain_cache():
    p = STATE / "no_chain.json"
    if not p.exists():
        return {}
    d = json.loads(p.read_text())
    cutoff = (NOW - dt.timedelta(days=7)).isoformat()
    return {k: v for k, v in d.items() if v > cutoff}


def fetch_chain(sym):
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
    for i in range(6):
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code == 200:
                return sym, r.json().get("data")
            if r.status_code in (403, 404):
                return sym, None            # genuinely no chain
            time.sleep(2 * (i + 1))         # 429 / 5xx: back off
        except Exception:
            time.sleep(2)
    return sym, "ERR"


def fetch_all_chains(universe):
    cache = load_no_chain_cache()
    syms = [s for s in universe if s not in cache]
    limit = os.environ.get("SCAN_LIMIT")
    if limit:
        syms = syms[: int(limit)]
    # resume file: if a run died mid-fetch, the next run within 12 min reuses what it got
    tmp = STATE / "chains_tmp.json"
    chains = {}
    if tmp.exists() and (time.time() - tmp.stat().st_mtime) < 12 * 60:
        chains = json.loads(tmp.read_text())
        syms = [s for s in syms if s not in chains]
        log(f"resuming: {len(chains)} chains already fetched")
    log(f"fetching {len(syms)} chains ({len(cache)} skipped from no-chain cache)")
    errors, done = [], 0
    with ThreadPoolExecutor(4) as ex:
        for sym, d in ex.map(fetch_chain, syms):
            done += 1
            if d == "ERR":
                errors.append(sym)
            elif d is None or not d.get("options"):
                cache[sym] = NOW.isoformat()
            else:
                chains[sym] = d
            if done % 100 == 0:
                log(f"  {done}/{len(syms)} fetched")
                tmp.write_text(json.dumps(chains))
                (STATE / "no_chain.json").write_text(json.dumps(cache))
    (STATE / "no_chain.json").write_text(json.dumps(cache))
    tmp.unlink(missing_ok=True)
    log(f"chains: {len(chains)} ok, {len(errors)} fetch errors {errors[:10]}")
    return chains


# ----------------------------------------------------------------------------
# 3. Candidate selection
# ----------------------------------------------------------------------------
def liquid(oi, bid, ask):
    L = CFG["liquidity"]
    mid = (bid + ask) / 2
    spr = ask - bid
    if oi < L["min_open_interest"]:
        return False
    return spr <= L["max_spread_abs"] or spr <= L["max_spread_pct_of_mid"] * mid


def tier_for(dte):
    w = CFG["windows"]
    if w["hot_dte"][0] <= dte <= w["hot_dte"][1]:
        return "hot"
    if w["next_dte"][0] <= dte <= w["next_dte"][1]:
        return "next"
    return None


def pick_candidates(sym, chain, info):
    S = CFG["selection"]
    px = chain.get("current_price") or info["price"]
    if not px:
        return []
    best = {}  # (side, tier) -> candidate
    for o in chain["options"]:
        m = OPT_RE.match(o["option"])
        if not m:
            continue
        exp = dt.datetime.strptime(m.group(2), "%y%m%d").date()
        dte = (exp - TODAY).days
        tier = tier_for(dte)
        if not tier:
            continue
        side = "call" if m.group(3) == "C" else "put"
        k = int(m.group(4)) / 1000
        if (side == "call" and k <= px) or (side == "put" and k >= px):
            continue
        dl = abs(o.get("delta") or 0)
        bid, ask = o.get("bid") or 0, o.get("ask") or 0
        if not (S["delta_min"] <= dl <= S["delta_max"]) or bid < S["min_bid"] or ask <= 0:
            continue
        mid = round((bid + ask) / 2, 3)
        collateral = k * 100 if side == "put" else px * 100
        roi = mid * 100 / collateral * 100
        if roi < CFG["roi"]["display_min_pct"]:
            continue
        c = dict(
            sym=sym, name=info["name"], side=side, tier=tier, px=round(px, 3), exp=str(exp), dte=dte,
            strike=k, delta=round(dl, 3), bid=bid, ask=ask, mid=mid, spread=round(ask - bid, 3),
            oi=int(o.get("open_interest") or 0), volume=int(o.get("volume") or 0),
            iv=round((o.get("iv") or 0) * 100, 1), theta=o.get("theta"), vega=o.get("vega"),
            collateral=round(collateral, 2), premium=round(mid * 100, 2),
            roi=round(roi, 2), roi_ann=round(roi * 365 / max(dte, 1), 0),
            cushion=round((px - k) / px * 100, 1) if side == "put" else round((k - px) / px * 100, 1),
            breakeven=round(k - mid, 3) if side == "put" else round(px - mid, 3),
            liquid=liquid(int(o.get("open_interest") or 0), bid, ask),
            chg=info.get("chg"), hi52=info.get("hi52"), lo52=info.get("lo52"), iv30=chain.get("iv30"),
        )
        key = (side, tier)
        # prefer liquid, then a strike that lands in the ROI target at <=.35 delta,
        # then closest to ideal delta, then higher roi
        T = CFG["roi"]
        on_target = T["target_min_pct"] <= roi <= T["target_max_pct"] and dl <= 0.35
        score = (c["liquid"], on_target, -abs(dl - S["ideal_delta"]), roi)
        if key not in best or score > best[key][0]:
            best[key] = (score, c)
    return [v[1] for v in best.values()]


# ----------------------------------------------------------------------------
# 4. Enrichment: realized vol, momentum, catalysts
# ----------------------------------------------------------------------------
def load_manual_catalysts():
    p = ROOT / "catalysts.csv"
    out = {}
    if not p.exists():
        return out
    with p.open() as f:
        for row in csv.reader(f):
            if not row or row[0].startswith("#") or row[0].lower() == "ticker":
                continue
            try:
                out.setdefault(row[0].upper(), []).append((dt.date.fromisoformat(row[1].strip()), row[2].strip()))
            except Exception:
                pass
    return out


def enrich(cands, universe):
    syms = sorted({c["sym"] for c in cands})
    hist = {}
    if syms:
        try:
            h = yf.download(syms, period="1y", interval="1d", auto_adjust=True, progress=False,
                            group_by="ticker", threads=True)
            for s in syms:
                try:
                    close = (h[s]["Close"] if len(syms) > 1 else h["Close"]).dropna()
                    lr = np.log(close).diff().dropna()
                    hv = float((lr.rolling(21).std() * np.sqrt(252) * 100).iloc[-1])
                    hist[s] = dict(hv30=round(hv, 1),
                                   ret1m=round(float(close.iloc[-1] / close.iloc[-22] - 1) * 100, 1) if len(close) > 22 else None,
                                   ret3m=round(float(close.iloc[-1] / close.iloc[-64] - 1) * 100, 1) if len(close) > 64 else None)
                except Exception:
                    pass
        except Exception as e:
            log(f"history download problem: {e}")
    manual = load_manual_catalysts()
    R = CFG["risk_flags"]
    for c in cands:
        u = universe[c["sym"]]
        h = hist.get(c["sym"], {})
        c.update(h)
        c["ivhv"] = round(c["iv30"] / h["hv30"], 2) if c.get("iv30") and h.get("hv30") else None
        exp = dt.date.fromisoformat(c["exp"])
        window_end = exp + dt.timedelta(days=R["catalyst_buffer_days"])
        flags, notes = [], []
        # earnings (Yahoo)
        ts = [t for t in (u.get("earnS"), u.get("earn")) if t]
        if ts:
            ed = dt.datetime.fromtimestamp(min(ts), dt.timezone.utc).date()
            c["next_earnings"] = str(ed)
            if TODAY - dt.timedelta(days=1) <= ed <= window_end:
                flags.append("EARNINGS")
                notes.append(f"earnings {ed}")
        # manual catalysts
        for d, note in manual.get(c["sym"], []):
            if TODAY <= d <= window_end:
                flags.append("CATALYST")
                notes.append(f"{note} ({d})")
        if c["px"] < R["sub_dollar_price"]:
            flags.append("SUB$1")
            notes.append("under $1: reverse-split / delisting risk")
        if h.get("ret3m") is not None and h["ret3m"] <= R["falling_knife_3m_pct"]:
            flags.append("KNIFE")
            notes.append(f"down {h['ret3m']}% in 3 months")
        if c["roi"] > CFG["roi"]["suspicious_above_pct"]:
            flags.append("TOO RICH")
            notes.append("premium this fat usually means an event the calendar missed")
        if not c["liquid"]:
            flags.append("THIN")
            notes.append(f"OI {c['oi']}, spread {c['spread']}")
        c["flags"], c["notes"] = flags, notes
        c["grade"] = grade(c)
    return cands


def grade(c):
    T = CFG["roi"]
    red = {"EARNINGS", "CATALYST", "SUB$1", "KNIFE", "TOO RICH", "THIN"}
    if red & set(c["flags"]):
        return "C"
    in_target = T["target_min_pct"] <= c["roi"] <= T["target_max_pct"]
    rich = (c.get("ivhv") or 0) >= CFG["risk_flags"]["ivhv_rich"]
    if c["tier"] == "hot" and in_target and rich and c["delta"] <= 0.35:
        return "A"
    return "B"


# ----------------------------------------------------------------------------
# 5. Output: JSON + dashboard
# ----------------------------------------------------------------------------
def write_outputs(cands, stats):
    cands.sort(key=lambda c: ({"A": 0, "B": 1, "C": 2}[c["grade"]], -c["roi"]))
    payload = dict(generated_utc=NOW.isoformat(timespec="seconds"),
                   generated_et=NOW.astimezone(ET).strftime("%a %b %d, %Y %I:%M %p ET"),
                   stats=stats, config=CFG, rows=cands)
    (ROOT / CFG["output"]["json"]).write_text(json.dumps(payload, indent=1))
    html = (ROOT / "dashboard_template.html").read_text().replace("__DATA__", json.dumps(payload))
    (ROOT / CFG["output"]["html"]).write_text(html)
    log(f"wrote {len(cands)} rows -> {CFG['output']['html']}")


# ----------------------------------------------------------------------------
# 6. Email new A-grades
# ----------------------------------------------------------------------------
def alert_new_a_grades(cands):
    if not CFG["alerts"]["email_on_new_a_grade"]:
        return
    user, pw, to = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD"), os.environ.get("ALERT_TO")
    p = STATE / "alerted.json"
    seen = json.loads(p.read_text()) if p.exists() else {}
    # forget keys whose expiration has passed
    seen = {k: v for k, v in seen.items() if k.split("|")[2] >= str(TODAY)}
    new = []
    for c in cands:
        if c["grade"] != "A":
            continue
        key = f"{c['sym']}|{c['side']}|{c['exp']}|{c['strike']}"
        if key not in seen:
            seen[key] = NOW.isoformat()
            new.append(c)
    p.write_text(json.dumps(seen))
    if not new:
        log("no new A-grades")
        return
    if not (user and pw and to):
        log(f"{len(new)} new A-grades but GMAIL_USER / GMAIL_APP_PASSWORD / ALERT_TO not set; skipping email")
        return
    rows = "".join(
        f"<tr><td><b>{c['sym']}</b></td><td>{c['side']}</td><td>${c['px']:.2f}</td><td>{c['exp']} ({c['dte']}d)</td>"
        f"<td>${c['strike']}</td><td>{c['delta']:.2f}</td><td>{c['bid']}/{c['ask']}</td><td>{c['oi']}</td>"
        f"<td><b>{c['roi']:.1f}%</b></td><td>{c['cushion']:.1f}%</td></tr>"
        for c in new[: CFG["alerts"]["max_rows_in_email"]])
    body = f"""<html><body style="font-family:Arial,sans-serif;font-size:14px">
<p>{len(new)} new A-grade setup{'s' if len(new)>1 else ''} as of {NOW.astimezone(ET).strftime('%I:%M %p ET')}.
Quotes are ~15 min delayed. Check the live chain on Robinhood before sending an order.</p>
<table border="1" cellpadding="5" cellspacing="0" style="border-collapse:collapse">
<tr style="background:#0b1f3a;color:#e8c46a"><th>Ticker</th><th>Side</th><th>Price</th><th>Exp</th><th>Strike</th>
<th>Delta</th><th>Bid/Ask</th><th>OI</th><th>ROI</th><th>Cushion</th></tr>{rows}</table>
<p style="color:#666">Dashboard: {os.environ.get('DASHBOARD_URL','(set DASHBOARD_URL secret)')}</p></body></html>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[CC Scanner] {len(new)} new A-grade: " + ", ".join(c["sym"] for c in new[:5])
    msg["From"], msg["To"] = user, to
    msg.attach(MIMEText(body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
        log(f"emailed {len(new)} new A-grades to {to}")
    except Exception as e:
        log(f"email failed: {e}")


# ----------------------------------------------------------------------------
def main():
    t0 = time.time()
    universe = build_universe()
    chains = fetch_all_chains(universe)
    cands = []
    for sym, chain in chains.items():
        cands += pick_candidates(sym, chain, universe[sym])
    log(f"candidates before enrichment: {len(cands)}")
    cands = enrich(cands, universe)
    stats = dict(universe=len(universe), with_chains=len(chains), candidates=len(cands),
                 a=sum(c["grade"] == "A" for c in cands), b=sum(c["grade"] == "B" for c in cands),
                 c=sum(c["grade"] == "C" for c in cands), runtime_s=round(time.time() - t0))
    write_outputs(cands, stats)
    alert_new_a_grades(cands)
    log(f"done in {stats['runtime_s']}s: A={stats['a']} B={stats['b']} C={stats['c']}")


if __name__ == "__main__":
    main()
