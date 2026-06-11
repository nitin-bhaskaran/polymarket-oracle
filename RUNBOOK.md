# RUNBOOK — Betfair paper-trading loop

Operational steps for starting, stopping, and checking the paper run.
This is the day-to-day "how do I run it again" guide. For what the project
*is*, see `README.md`.

> All commands assume Windows PowerShell, from the repo root
> `D:\Projects\polymarket-oracle`, with the virtualenv active:
> ```powershell
> cd D:\Projects\polymarket-oracle
> .\.venv\Scripts\Activate.ps1
> ```

---

## 1. Before you start (one-time / after any pull)

**a. Make sure the code is current and the cost-controlled config is loaded.**
`git pull` updates `config/config.example.yaml` (tracked) but NOT your
`config/config.yaml` (gitignored) — they silently drift. After any pull that
might touch config, re-sync:

```powershell
git pull origin main
Copy-Item config\config.example.yaml config\config.yaml -Force
```

> Safe because secrets live in `.env`, not in `config.yaml` (which only holds
> placeholder credentials). If you have hand-edited `config.yaml`, edit the
> values by hand instead of overwriting.

**b. Confirm the cost-control values are present** (expect 5 lines):

```powershell
Select-String -Path config\config.yaml -Pattern "scan_interval|daily_deep_assessment_budget|web_search_max_uses|reassess_"
```

Expected:
- `daily_deep_assessment_budget: 30`   (hard cost ceiling ≈ $3/day)
- `web_search_max_uses: 2`             (main cost driver — keep low)
- `reassess_after_hours: 12.0`
- `reassess_on_move: 0.10`
- `scan_interval: 2700`                (45-min cycle; a SINGLE line, no duplicate)

**c. Credentials are in `.env`** (NOT committed). Required keys:
`BETFAIR_APP_KEY`, `BETFAIR_USERNAME`, `BETFAIR_PASSWORD`,
`ANTHROPIC_API_KEY`, and optionally `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.

**d. VPN OFF.** Betfair geo-blocks non-UK IPs. If a VPN is routing you through
another country you will get blanket `403 Forbidden` on login. Turn the VPN off
(or split-tunnel to exclude betfair.com / identitysso.betfair.com).

**e. Disable sleep** (so the loop does not pause):
Settings → System → Power → "When plugged in, put my device to sleep after" →
**Never**. On a laptop also set lid-close (plugged in) → **Do nothing**.

---

## 2. Sanity check (proves config + credentials + UK IP all work)

```powershell
python -m core.betfair_main --scan-once
```

If it logs in and prints a list of markets, you are good to run. If it shows
`403 Forbidden` on login → VPN is on or you are on a non-UK IP.

---

## 3. Start the paper run

```powershell
python -m core.betfair_main --paper
```

What a healthy run looks like:
- `=== Paper cycle #N ===` lines advancing (~45 min apart)
- deep assessments showing `2 search(es)` (not 3)
- `PLACED paper ...` lines when it places a bet
- when the daily cap is hit: `Daily deep-assessment budget exhausted (30)`
- occasional `429` that self-recovers via backoff is normal

State persists to `data/paper_bets.jsonl` and `data/governor.json`, so the run
survives restarts. **The daily budget does NOT reset on restart** (deliberate).

---

## 4. Stop the run

Press **Ctrl-C once** in the running window, then WAIT a few seconds for it to
finish the current operation and print `Shutdown requested`. Do not mash Ctrl-C
(repeated interrupts can abort mid-write). State is flushed atomically, so a
single clean Ctrl-C never corrupts data.

To restart later, just run the `--paper` command again — it resumes from disk.

---

## 5. Check results (read-only, run anytime in a separate window)

```powershell
python -m core.paper_analysis
```

Reports per-slice win rate, ROI-on-risk, Brier score, calibration table, and a
£100-bankroll simulation. The Brier output is shown as `AI/mkt`; positive
`skill` means the AI probability beat Betfair's probability baseline, while
negative skill means the market was better. Results are sliced by strategy
sleeve, domain, competition, and market type.

**Results are only meaningful at ~50–100 settled
bets** — below that, treat every number as noise. Watch for:
- the £100 equity line climbing vs bleeding
- positive Brier skill against the Betfair baseline
- any single slice persistently positive at decent n (a possible real edge)

---

## 6. Other modes (diagnostics)

```powershell
python -m core.betfair_main --scan-once      # list markets, no assessment
python -m core.betfair_main --assess-once    # triage only (no web search)
python -m core.betfair_main --deep-once      # one full deep web-search assess
python -m core.betfair_main --list-politics  # list political markets
```

---

## 7. Cost control

The hard ceiling is `daily_deep_assessment_budget` (≈ $0.10/deep assessment,
so 30 ≈ $3/day). To spend less, lower it. Also set an account-level backstop in
the Anthropic Console (Settings → Limits) in case config ever drifts again.
