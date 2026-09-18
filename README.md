# Premium Scanner

Runs every 30 minutes during market hours on GitHub Actions, scans every optionable
US stock between $0.50 and $10, and publishes a live dashboard of cash-secured puts
and covered calls paying 3–10% ROI in 3–10 days (plus an 11–21 day tier).
Emails you when a new A-grade shows up.

## One-time setup (about 10 minutes)

1. **Create a new GitHub repo** (private is fine) and upload every file in this folder,
   keeping the folder structure (`.github/workflows/scan.yml` must stay where it is).

2. **Turn on GitHub Pages.** Repo → Settings → Pages → Source: *Deploy from a branch* →
   Branch: `main`, folder: `/docs` → Save. Your dashboard URL will be
   `https://<your-username>.github.io/<repo-name>/`. It's live after the first run.

3. **Let Actions push to the repo.** Settings → Actions → General → Workflow permissions →
   *Read and write permissions* → Save.

4. **Email alerts (optional).** Settings → Secrets and variables → Actions → *New repository secret*, add:
   - `GMAIL_USER` — the Gmail address that sends the alert
   - `GMAIL_APP_PASSWORD` — a 16-character Google *App Password*
     (Google Account → Security → 2-Step Verification → App passwords). Not your normal password.
   - `ALERT_TO` — where the alerts go (can be the same address)
   - `DASHBOARD_URL` — the Pages URL from step 2, so the email links to it

   Skip this step and the scanner still runs; it just won't email.

5. **Kick it off.** Actions tab → *premium scan* → *Run workflow*. First run takes 5–10 minutes.
   After that it runs itself on the schedule.

## Tuning it

Everything is in `config.json`: price range, delta band, liquidity floor, ROI targets, flag
thresholds. Change a number, commit, and the next run uses it.

- `watchlist.txt` — tickers that are always scanned, even if they fall below the volume floor.
- `catalysts.csv` — drug readouts, PDUFA dates, court rulings, votes. Yahoo only knows earnings
  dates, so add anything binary here and it'll get a red flag inside its window.

## Running it on your own PC instead

```
pip install -r requirements.txt
python scanner.py
```
Then open `docs/index.html`. `SCAN_LIMIT=60 python scanner.py` does a quick test on 60 names.

## How it grades

- **A** — 3–10 day window, ROI in the 3–10% band, option IV above the stock's realized vol
  (you're being overpaid for the movement), delta ≤ .35, no red flags.
- **B** — tradeable but misses one of those: 11–21 day tier, ROI a bit under target, IV fair
  rather than rich, or delta up to .40.
- **C** — a red flag: earnings or a listed catalyst inside the window, price under $1,
  down 50%+ in 3 months, premium suspiciously fat (>12%), or a thin chain.

Every number is ~15 minutes delayed (CBOE delayed quotes). Confirm the live chain on
Robinhood before you send an order.

## Known limits

- Yahoo's earnings calendar misses biotech readouts. `catalysts.csv` is the fix.
- 3–10 DTE only exists on names with weekly expirations; the rest surface in the 11–21 tier
  the week after a monthly expiry.
- Free GitHub Actions gives 2,000 minutes/month. This uses roughly 800–1,000. If a run
  times out, drop `min_avg_volume` frequency by changing the cron to every hour.
