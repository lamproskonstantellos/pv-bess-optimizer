Introduction
============

**pv-bess-optimizer** is a Mixed-Integer Linear Programming (MILP) model
for PV + BESS sizing and hourly dispatch, with a multi-year project-
finance pipeline and rolling-horizon Monte Carlo for uncertainty
analysis.

Two regulatory regimes are supported:

* ``self_consumption`` — Greek Self-consumption with co-located load.  Load
  balance, load priority (binary-free slack formulation), no
  simultaneous grid I/O (tight big-M), retail tariff for self-
  consumption, DAM for export.
* ``merchant`` — pure utility-scale dispatch with **no co-located load**.
  PV and BESS dispatch entirely to the day-ahead market.

The hard static max-injection cap on grid-bound flows is enforced in
both modes per **MD YPEN/DAPEEK/53563/1556/2023** (73 % allowed for
distribution-connected, 72 % for transmission-connected; equivalently
27 % / 28 % of the cap remains unused as curtailment).
