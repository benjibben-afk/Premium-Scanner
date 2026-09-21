#!/usr/bin/env python3
"""
Keeps the premium scanner running inside GitHub Actions, no outside timer needed.

- Scans at :15 and :45 past every hour while the market is open (9:45 AM - 3:45 PM ET),
  so each scan sees live quotes (CBOE data is ~15 min delayed).
- Sleeps overnight, on weekends and on market holidays.
- A single GitHub run can last 6 hours max, so every ~5h40m this starts a fresh run
  of itself and exits. The chain keeps going on its own.
- Off switch: set "autoscan": false in config.json. The loop sees it at its next
  check and stops without restarting itself.
"""
import datetime as dt
import json
import signal
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BUDGET_MIN = 340          # hand off well before GitHub's 360-minute job limit
SCAN_MINUTES = (15, 45)   # scan slots each hour
FIRST_SCAN = dt.time(9, 45)
START = time.time()

# NYSE full-day closures. Extend this list each year.
HOLIDAYS = {
    "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}  # 1:00 PM ET close

cancelled = False


def on_term(signum, frame):
    # Someone hit "Cancel workflow" in GitHub: stop and do NOT restart.
    global cancelled
    cancelled = True
    raise SystemExit(1)


signal.signal(signal.SIGTERM, on_term)
signal.signal(signal.SIGINT, on_term)


def now():
    return dt.datetime.now(ET)


def log(msg):
    print(f"[{now().strftime('%a %H:%M:%S ET')}] {msg}", flush=True)


def session(day):
    """(first scan time, close) for a trading day, or None if the market is closed."""
    if day.weekday() >= 5 or day.isoformat() in HOLIDAYS:
        return None
    close = dt.time(13, 0) if day.isoformat() in EARLY_CLOSE else dt.time(16, 0)
    return (dt.datetime.combine(day, FIRST_SCAN, ET), dt.datetime.combine(day, close, ET))


def current_or_next_session(t):
    for i in range(14):
        s = session(t.date() + dt.timedelta(days=i))
        if s and s[1] > t:
            return s
    raise RuntimeError("no trading session found in the next two weeks")


def next_slot(t):
    """Next :15 or :45 strictly after t."""
    base = t.replace(second=0, microsecond=0)
    for add in range(1, 61):
        cand = base + dt.timedelta(minutes=add)
        if cand.minute in SCAN_MINUTES:
            return cand
    return base + dt.timedelta(minutes=30)


def remaining():
    return BUDGET_MIN * 60 - (time.time() - START)


def sleep_until(target):
    """Sleep until target. Returns False if that would run past this run's time budget."""
    secs = (target - now()).total_seconds()
    if secs > remaining() - 60:
        return False
    if secs > 0:
        log(f"sleeping until {target.strftime('%a %b %d %I:%M %p ET')}")
        time.sleep(secs)
    return True


def sh(cmd, check=True):
    return subprocess.run(cmd, shell=True, check=check)


def sync():
    """Pull the latest code and config so edits made on GitHub take effect next scan."""
    sh("git fetch -q origin main && git reset -q --hard origin/main")


def autoscan_on():
    try:
        return json.loads(Path("config.json").read_text()).get("autoscan", True)
    except Exception:
        return True


def publish():
    stamp = now().strftime("%Y-%m-%d %H:%M ET")
    for _ in range(4):
        sh("git fetch -q origin main && git reset -q --mixed origin/main")
        sh("git add docs state")
        if sh("git diff --cached --quiet", check=False).returncode == 0:
            log("nothing new to publish")
            return
        sh(f'git commit -q -m "scan {stamp}"')
        if sh("git push -q origin HEAD:main", check=False).returncode == 0:
            log("dashboard published")
            return
        log("push collided, retrying")
        time.sleep(5)
    log("could not publish after 4 tries")


def run_scan():
    try:
        sh("python scanner.py")
        publish()
    except SystemExit:
        raise
    except Exception as e:
        log(f"scan attempt failed, will try again next slot: {e}")


def main():
    handoff = True
    try:
        while True:
            sync()
            if not autoscan_on():
                log("autoscan is false in config.json: stopping, not restarting")
                handoff = False
                return
            t = now()
            open_at, close_at = current_or_next_session(t)
            if t < open_at:
                if not sleep_until(open_at):
                    return
                continue
            if remaining() < 25 * 60:
                return  # not enough time for a full scan; the next run picks it up
            slot_start = now()
            log("scanning")
            run_scan()
            nxt = min(next_slot(slot_start), close_at)
            if not sleep_until(nxt):
                return
    finally:
        if cancelled:
            log("run was cancelled by hand: not restarting")
        elif handoff:
            log("handing off to a fresh run")
            sh("gh workflow run main.yml --ref main", check=False)


if __name__ == "__main__":
    main()
