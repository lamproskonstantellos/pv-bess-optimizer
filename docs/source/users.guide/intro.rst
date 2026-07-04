Introduction
============

**pv-bess-optimizer** is created and developed by
**Lampros Konstantellos**.

It is a Mixed-Integer Linear Programming (MILP) model
for sub-hourly dispatch optimization (15-minute canonical cadence,
auto-detected from the timeseries), with PV + BESS capacity sizing (a
sweep over the dispatch solve), a multi-year project-finance pipeline
and rolling-horizon Monte Carlo for uncertainty analysis.

Two regulatory regimes are supported:

* ``self_consumption``: self-consumption with co-located load.  Load
  balance, load priority (binary-free slack formulation), no
  simultaneous grid I/O (tight big-M), retail tariff for self-
  consumption, DAM for export.
* ``merchant``: pure utility-scale dispatch with **no co-located load**.
  PV and BESS dispatch entirely to the day-ahead market.

A hard static (or hourly-profiled) max-injection cap on grid-bound
flows is enforced in both modes.  The allowed-injection percentage is
a plain user input (``max_injection`` sheets), so any national or
contractual curtailment rule can be modelled by entering its value.
