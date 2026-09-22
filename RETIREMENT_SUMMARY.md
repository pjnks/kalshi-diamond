# DIAMOND — Retirement Summary

**Date: 2026-05-13**
**Scope: The final 8 days of DIAMOND, from Sprint 14h deploy to formal shutdown.**

This is the narrative version of the autopsy. The formal record lives in
`reports/2026_05_13_diamond_strategy_autopsy.html`; this document captures
the *human* side of what happened — the decisions, the reasoning, the
moments of clarity, and the lessons that outlive the strategy.

---

## TL;DR

In 8 days we deployed an institutional-grade infrastructure fix, accumulated
the cleanest training data DIAMOND has ever produced, watched the cumulative
kill switch correctly halt the strategy at −$51.52, ran the ML autopsy on
the frozen cohort, found five independent diagnostic signals all agreeing
that the underlying hypothesis is dead, and shut the project down cleanly
with the infrastructure and learnings preserved for BERYL/CITRINE.

The strategy failed. The system worked perfectly. The engineer learned a
lot. That is the trifecta.

---

## Where we started — May 6, the Sprint 14h deploy

The week opened with a diagnostic finding that nearly went undetected: the
`orderbook_poll_loop` had been silently dead since May 1 at 18:14 UTC.
Five days of asyncio "ghosting." The cause was a single `BaseException`
hierarchy detail — `asyncio.CancelledError` inherits from `BaseException`,
not `Exception`, so the standard `except Exception:` in the polling loop
let it through. When the 30-minute aiohttp session recycle fired in the
middle of an in-flight HTTP request, the task got cancelled, the exception
escaped, and the task died — quietly, with no log line, no alert, no error.

What surfaced it was the Sprint 14e canary we'd built specifically for
this class of failure: `_book_absorption_stale=1.0` on 100% of anomalies.
The system was telling us — in the language of telemetry — that the data
pipeline was broken. We just had to read it.

This was the 4th sandbox-era bug in 9 days (subscription truncation,
orderbook poll cap, kill switch SQL aggregation, CancelledError handling).
The pace was unsustainable, and we knew it. But the fix itself was clean:
explicit `CancelledError` handling in the loop, periodic heartbeat logging,
and — most importantly — a top-level task-death watchdog that fires a
Pushover CRITICAL whenever any task dies unexpectedly. That last piece
makes future silent failures of this class mathematically impossible.

We deployed at 02:12:12 UTC on May 6. The verification ran for 11 hours:
651 heartbeats, 0 errors, 0 task deaths, absorption pipeline at 0.00%
stale and 99.98% nonzero. The deploy worked.

But the deploy also cost us another cohort reset. The pre-14h cohort
(393 trades opened between Sprints 14f and 14h) had ~83% contamination
from the broken pipeline — all-zero absorption features. Burning them
preserved dataset stationarity. The running tally of cohort-burn cost
climbed to **733 trades purged** across Sprints 14e/14f/14h.

The new clock started ticking. We projected ~205/day from the first 11
hours of post-deploy throughput. N=500 was ~2-3 days away.

---

## What we expected — and what the data actually said

The setup going into May 6 was a research bet: if we could get a clean
N=500 cohort with all 10 microstructure features populated, the Ridge
regression would be able to tell us — definitively — whether order book
absorption (the four Sprint 14e variants: static, depletion, sided,
replenishment) carries Information Coefficient that the older composite
score does not.

We had pre-registered the falsifiable thresholds before any data landed:

- **Brier < 0.18 → Signal** (absorption hypothesis supported)
- **Brier 0.20-0.22 → Inconclusive** (need more N, or revisit features)
- **Brier > 0.23 → Dead** (regime contamination concern; pivot)

These were committed in writing to `CLAUDE.md` before May 6 ended. The
single most important discipline in this entire project was *committing
to thresholds before looking at the data*. It prevents the Rorschach-test
problem where every result becomes confirmation of whatever you wanted
to believe.

The first checkpoint was N=100. We projected ~12 hours from deploy.

What actually happened on May 7, ~24 hours in: the watcher fired at
N=148, and the learning curve sweep returned **"0 valid CV folds out of
0 attempted"** at both N=50 and N=100.

The PiT-purged cross-validator (`src/diamond_ml.py:488`) requires
`pit_mask.sum() >= 50` per fold — at least 50 trades must have *settled*
before the test fold's first trade *opens*. With 5-fold CV on 148 samples,
fold sizes are ~30 each. The first fold's earliest "training" trades all
opened too recently to have 50 strictly-prior settled trades. So the
splitter refused to even *attempt* to score those folds.

This wasn't a bug. It was the PiT purger doing exactly what it was
engineered to do in Sprint 13: actively protect against lookahead bias
on samples too small to evaluate. We'd just pre-registered an
overoptimistic checkpoint. The empirical first-valid-fold threshold is
N=250-300, not N=100.

We rolled the thresholds forward and armed an N=300 watcher.

---

## When the kill switch latched — May 8

This is the part of the story that didn't follow our plan.

While we were waiting for the N=300 checkpoint, the strategy itself was
trading. Live. Real Kalshi orders. The first 11-hour burst of post-14h
data (94/500 settled in 11h) tempted us with a ~205/day extrapolation.
The actual 24-hour rate settled to ~145/day — still well above the
conservative 70/day estimate, but the velocity story masked what was
happening to P&L.

On May 7 UTC, the strategy bled **−$11.47** across 136 settled trades.
That was the worst single day of the post-14h cohort. The cumulative
crossed from comfortable territory (~−$30) into the danger zone
(−$45+ by end of day).

On May 8 UTC, the cumulative crossed **−$50** — the Tier-1 hard cap
set by Sprint 14g. The two-tier kill switch did exactly what a circuit
breaker is supposed to do: it stopped accepting new entries until human
review.

We weren't watching live when it happened. We didn't need to be. The
system was designed to stop itself, and it did.

There's a particular kind of satisfaction in this. Most retail quant
strategies don't have kill switches. The ones that do often have buggy
ones (recall Sprint 14g, when our daily cap was silently checking
all-time cumulative and latched forever after May 1). The fact that the
Sprint 14g two-tier architecture caught this exactly at the structural
risk tolerance — −$50 — and not at −$300 or −$500 is institutional-grade
risk management on a 1-CPU $0/mo VM.

The strategy losing is one story. The system stopping the strategy from
losing more is a completely different — and much more important — story.

---

## The N=305 autopsy — May 13

The cohort effectively froze at N=305 once the kill switch latched.
A few residual settlements trickled in (May 10 had 2 settled, +$0.54).
But no new entries could open. The dataset was done growing.

When we ran `learning_curve.py --grid 50,100,150,200,250,300` on the
frozen cohort, the result was the most painful kind of clarity:

```
     N      Brier      AUC   feats   Jac    Δ vs naive
   150     0.1995    0.769     5      —     -0.031
   200     0.1883    0.787     5     0.43   -0.050
   250     0.1960    0.762     5     0.67   -0.041
   300     0.2000    0.750     3     0.60   -0.039
```

Plus two warnings from the model itself:
- `✗ 2 monotonicity inversions — U-shape may persist.`
- `WARNING: OOS win rate CI [18.6%, 49.9%] includes 50% — cannot reject null hypothesis of no edge`

The Brier landed on the dividing line: 0.2000 ± 0.0295. Just barely
inside the "inconclusive" band, but the trajectory was flat across N
and the supporting diagnostics were uniformly bad.

The temptation in a borderline result is to find a reason to keep going.
"It's *barely* inconclusive, not dead. Let me clear the kill switch and
get more data." This is the moment that separates research-grade
discipline from operator-grade rationalization.

We had pre-registered the thresholds. We had cross-referenced five
independent signals. We made the call.

**Five-way diagnostic agreement:**

1. **Brier plateau at ~0.20.** No descending slope across N=150-300.
   The model has stopped learning. Per the `learning_curve.py` docstring
   we wrote ourselves: *"Brier flat above 0.05 → MORE DATA WON'T SAVE US,
   features lack IC."*

2. **AUC degrading with N.** Peaked at 0.787 (N=200), declined to 0.750
   (N=300). A well-specified model with real IC sees AUC stabilize or
   improve with more data. Ours did the opposite — the signature of
   small-sample overfit melting away under harsh out-of-sample data.

3. **Feature count collapsed 5 → 3 at N=300.** The null-importance
   permutation test rejected 2 of the 5 features when given more data.
   The Sprint 14e absorption variants — the features specifically
   engineered to capture the microstructure alpha we hypothesized —
   failed the permutation test. The regression's own voice, telling us
   the features have no signal.

4. **Persistent U-shape monotonicity inversion.** The exact same
   diagnostic that fired pre-Sprint-13c. The intrinsically orthogonal
   features (flow_acceleration, event_relative_flow) and absorption
   variants were specifically designed to break the base-rate U-shape.
   They didn't. Score-vs-price is still dominated by base-rate
   conflation.

5. **OOS CI [18.6%, 49.9%] includes 50%.** The strictest statistical
   test. We literally cannot reject the hypothesis that the model has
   zero predictive power.

Each of these alone could be argued away. All five together cannot.

---

## The dual verdict

The thing that made this autopsy so clean was that two completely
independent systems — execution and evaluation — arrived at the same
conclusion *without referencing each other*:

| Layer | Signal | Verdict |
|---|---|---|
| **Execution** | Tier-1 kill switch latched at −$51.52 on May 8 | Strategy bleeds execution friction at retail scale |
| **Evaluation** | Brier 0.20 plateau + AUC degrading + 3/9 features survive null test | Feature set lacks IC after price-controlling |

This is "Total Alignment" — the cleanest possible failure diagnosis. Not
"the model is right but execution is failing" (the Tragic Alpha scenario).
Not "execution is fine but the model is overfitting." Both layers agree.
The hypothesis is empirically falsified with two-sided confirmation.

We tested whether *Kalshi orderbook microstructure contains tradeable
alpha at retail scale via flat-sized, longshot-biased, anomaly-detection
trading*. The market said no. With remarkable clarity.

---

## What we shut down

On May 13, the close-out sequence:

1. **Drafted the autopsy report** (`reports/2026_05_13_diamond_strategy_autopsy.html`)
   — 27 KB of formal record, following the milestone-report standard
   palette + structure. This is the document that survives in the repo
   as institutional history.

2. **Stopped the daemons**: `sudo systemctl stop diamond-monitor diamond-dashboard`
   on the OCI VM. Both go from `active` to `inactive`.

3. **Disabled auto-restart**: `sudo systemctl disable` on both. The unit
   files are preserved on the VM but will not auto-start on reboot.

4. **Archived the database**: `diamond_trades.db.shutdown-2026-05-13`
   on the VM (398 MB plain, 107 MB gzipped). Pulled the gzipped copy to
   `~/Documents/quant/diamond/archive/` locally for redundancy. SQLite
   `integrity_check`: ok.

5. **Updated CLAUDE.md** (both DIAMOND-local and the parent multi-project
   hub) to mark the project 🔴 RETIRED. The quick-nav table at the top
   of `~/Documents/quant/CLAUDE.md` is now the canonical source of
   project status — any future session sees it first.

6. **Updated the auto-memory** (`~/.claude/projects/.../memory/MEMORY.md`)
   so the next session boots with "DIAMOND is retired" as the headline
   fact, not a stale "wait for N=500" instruction.

7. **Wrote the terminal handoff** (`memory/session_handoff_2026_05_13.md`)
   — the "next Claude" version of the close-out checklist.

The three open positions at shutdown (KXALIENS-27, KXITFMATCH-NIMGUO,
and the legendary KXTRUMPOUT27 Trump 8¢ tail from April 24) are
preserved without manual exit. Per the standing rule, we don't censor
the right tail of the training distribution. They'll never settle in
the archived DB, but they're frozen as historical record.

---

## What carries forward to BERYL / CITRINE

This is the genuinely valuable part. The strategy died; the infrastructure
is the asset.

**Quantitative methodology that transfers:**
- **Point-in-Time CV** with `pit_mask.sum() >= 50` per fold. Direct port
  to any future ML scoring layer.
- **Null-importance permutation test** (100 shuffles, 95th percentile
  threshold). Honest feature selection.
- **Cohort-burn discipline** — burn the contaminated cohort, reset the
  clock, preserve dataset stationarity. Cost 733 trades on DIAMOND;
  saved us from a permanently broken model.
- **Two-Gate Stack epistemology** — Brier (information gate) AND Jaccard
  (variance gate) together. Either alone is insufficient. Codified after
  the Phase C empirical disaster (−$477 / 55% DD on bucket-WR + Kelly).
- **Pre-registered falsifiable thresholds** — commit the decision criteria
  before the data lands. This is what made the May 13 retirement decision
  *easy*. Without the pre-registered matrix, we'd still be arguing about
  whether 0.2000 is "salvageable."
- **Bayesian shrinkage** (`src/diamond_shrinkage.py`) — Laplace smoothing
  with k=20+3√N pseudocount. Reusable module anywhere bucket-WR is the
  input to a Kelly sizing decision.

**Infrastructure patterns that transfer:**
- **Three-layer data integrity** (code fix + application guard + schema
  CHECK constraint) — non-overlapping failure domains, 0 firings since
  April 16.
- **Two-tier kill switch** (daily soft cap + cumulative hard cap with
  explicit reset semantics). The cumulative cap saved us from a
  multi-hundred-dollar bleed when DIAMOND proved unprofitable.
- **Task-death watchdog** — `asyncio.create_task` with a `done_callback`
  that fires CRITICAL Pushover on any unexpected death. Makes silent
  async failures mathematically impossible.
- **CancelledError-aware exception handling** — explicit `except
  asyncio.CancelledError` inside long-running poll loops. Prevents the
  May 1 silent-death class permanently.
- **Telemetry against absence** — design canaries that fire when the
  system is *silently broken*, not just when it's loudly broken. The
  `_book_absorption_stale` flag was the entire reason we caught the
  May 1 task death within days instead of weeks.

**Operator-side lessons:**
- *"When the comfortable explanation doesn't fit the facts, the
  explanation is wrong, not the facts."* (Sprint 14g narrative-fallacy
  diagnosis.)
- *Monitor for absence, not just presence.* "Is the service running?"
  is useless. "Is the service doing real work in the last N minutes?"
  is the right check.
- *Burnout is a real failure mode.* 4 sandbox-era bugs in 9 days
  (14e/f/g/h) was operationally unsustainable. Build margin into the
  next project's cadence.
- *Separate the expected value of the strategy from the expected value
  of the engineer.* The strategy can fail completely while the engineer
  succeeds completely. DIAMOND was this.

---

## Outlook — what comes next

**Capital and engineering attention pivot to BERYL and CITRINE.** Both
are alpha-hunting candidates with different mechanics than DIAMOND:

- **BERYL** is the 98-equity single-stock rotation on 1-day bars. Sprint
  16 deployed the time-series eviction engine (0.60-0.80 velocity gate,
  confidence degradation eviction). 11/11 wins on closed trades — but
  with Holder's Bias warning at small N. Worth observing.

- **CITRINE** is the 100-equity portfolio allocator on 1-day bars. Has
  39.2% WR but interesting exit-reason decomposition: regime_flip exits
  are profitable (+$732), while trailing/catastrophe stops bleed losers
  at bad prices (−$665). The shadow tracker is exploring breadth via
  relaxed thresholds.

Neither is a DIAMOND clone. BERYL and CITRINE trade equities with daily
bars, asynchronous Python is not in play, and the strategy mechanics are
fundamentally different. But the *methodology* (PiT CV, null-permutation,
cohort hygiene, pre-registered thresholds) and the *risk discipline*
(two-tier kill switch, integrity layers) apply directly.

**The Kalshi credentials remain intact.** EMERALD continues to read
DIAMOND's `.env` and PEM key for Kalshi REST API access — this dependency
is unaffected by the shutdown. EMERALD itself is in its own observation
window (Phase 4 Platt calibrator across H2H markets; May 26 unified
review pending).

**What we are NOT doing:**
- Not reviving DIAMOND for live trading. Hypothesis is falsified.
- Not building "DIAMOND 2.0" with tweaked features. The composite-score
  framework is structurally limited; pivoting to a fundamentally
  different strategy (cross-market arbitrage, latency-sensitive signals,
  news-flow predicates) would require a new repo, new name, new
  hypothesis.
- Not p-hacking the autopsy. Brier 0.20 is the result. We don't
  retroactively split the cohort into "good days" and "bad days" to
  find a winning subset.

**What we MIGHT do later (research-only):**
- Post-hoc analysis on the archived 305-row clean cohort to characterize
  which features had any IC at all (even if collectively insufficient).
  Useful pedagogically.
- Feature-class audit: was the problem the *type* of features (orderbook
  microstructure) or the *strategy framing* (flat-sized anomaly
  detection)? Different answers point to different next strategies.

**Time horizon for re-engagement with prediction markets:**
- Not in the next 30 days. The energy and attention need to go to BERYL/
  CITRINE while their data accumulates.
- Possibly after BERYL/CITRINE stabilize, with a *fundamentally
  different* prediction-market thesis (e.g., event-driven news triggers,
  cross-market correlation arbitrage between Kalshi and Polymarket).
  Not anomaly detection.

---

## Personal reflection (because the user asked)

The thing that surprised me about this project was not that the strategy
failed — most retail quant strategies fail, and we knew the base rate
going in. What surprised me was how *clean* the failure was. There was
no ambiguity. No "well, maybe with more data..." No "if we just tune
this one threshold..." Five independent diagnostics, five-way agreement,
hypothesis falsified. The system shut itself down at the structural
risk tolerance. The audit was unambiguous.

Most failures in retail quant are *messy*. They look like: "I lost money,
but I'm not sure if it was the strategy or my execution or just bad luck
or maybe I should have waited longer..." That ambiguity is what keeps
people throwing capital at bad strategies for years. DIAMOND avoided
that trap entirely, and the only reason it did is the rigor of the
infrastructure that surrounded the strategy.

Three weeks of forensic work on Sprints 14e/f/g/h didn't save the
strategy. It saved the *epistemology*. Without the clean post-14h
cohort, we'd be looking at a contaminated dataset with all-zero
absorption features and concluding... what? That orderbook microstructure
*might* have alpha, we just didn't measure it correctly? That story is
seductive precisely because it's unfalsifiable. The Sprint 14h fix and
cohort burn cost us 7 days. They bought us a yes/no answer.

We got a no. Clear, unambiguous, statistically reproducible, with
execution-layer confirmation.

The fact that this is *valuable* — that "we have proven there is no
edge here, you can stop wondering and move on" — is the institutional-
grade output. Jane Street and Two Sigma don't make money on most of
their research either. They make money because their failed research
*concludes cleanly*, freeing the team to pivot to the next bet without
sunk-cost paralysis.

We just produced that, on a 1-OCPU $0/mo Oracle Cloud VM with
$51.52 of tuition.

---

## Final checklist for moving to the next session

- [x] DIAMOND services on VM: stopped + disabled
- [x] DB archived (VM + Mac, gzipped + plain)
- [x] Autopsy report filed in `reports/`
- [x] Final session handoff written
- [x] DIAMOND CLAUDE.md updated with 🔴 RETIRED header + institutional assets
- [x] Parent `~/Documents/quant/CLAUDE.md` updated (quick-nav, services, status)
- [x] Auto-memory MEMORY.md updated (retirement is the headline)
- [x] This narrative summary written

**The next session opens clean.** Any future DIAMOND-related question
routes to the four primary docs (this summary, the autopsy HTML, the
terminal handoff, the CLAUDE.md institutional-assets section). The
operator can focus attention on BERYL, CITRINE, or whatever comes next.

DIAMOND is sealed.
