# Changelog

## 0.9.0 — Current state (unreleased)

Feature-complete pre-release.  No prior versions have shipped; no
compatibility surface is maintained.

### Capabilities

- Co-optimised dispatch of PV + BESS in two modes
  (`self_consumption` and `merchant`) and three asset configurations
  (`hybrid`, `pv_only`, `bess_only`).
- Stochastic balancing market participation across FCR, aFRR, and
  mFRR with per-product capacity reservation, expected-value MILP,
  and Monte Carlo realisation.
- Project finance pipeline: lifetime cashflow, NPV, IRR, ROI, BCR,
  LCOE, LCOS, payback.  Inflation indexation per revenue / cost
  stream.
- IEEE-styled PDF reporting with full plotting suite, including
  per-product balancing revenue breakdowns (yearly stack, per-month
  BESS revenue, BESS revenue waterfall, capacity-vs-activation split).
- Rolling-horizon Monte Carlo with log-normal forecast noise on DAM,
  PV, load, and balancing prices.
- One-at-a-time sensitivity tornado.

### Solver

- Pyomo + HiGHS (default).
