.. pv-bess-optimizer documentation master file

pv-bess-optimizer: PV + BESS dispatch optimizer
================================================

**pv-bess-optimizer** is a Mixed-Integer Linear Programming (MILP) model
for sub-hourly dispatch optimization (15-minute canonical cadence,
auto-detected from the timeseries), with PV + BESS capacity sizing (a
sweep over the dispatch solve), a multi-year project-finance pipeline
and rolling-horizon Monte Carlo for uncertainty analysis.

Two regulatory regimes are supported:

* ``self_consumption``: Greek Self-consumption with co-located load.
* ``merchant``: pure utility-scale dispatch with no co-located load.

The hard static max-injection cap on grid-bound flows is enforced in
both modes per **MD YPEN/DAPEEK/53563/1556/2023**.

The codebase is pure Python and runs on Windows, macOS, and Linux with
Python ≥ 3.11.

.. toctree::
   :maxdepth: 2
   :caption: User's manual

   users.guide/intro
   users.guide/install
   users.guide/inputs
   users.guide/running
   users.guide/outputs
   users.guide/output_layout
   users.guide/economics
   users.guide/financial_plots
   users.guide/sensitivity
   users.guide/rolling_horizon

.. toctree::
   :maxdepth: 2
   :caption: Technical documentation

   technical.documentation/model
   technical.documentation/objectives
   technical.documentation/kpis
   technical.documentation/energy-balance
   technical.documentation/lifetime_scaling
   technical.documentation/mip_formulation
   technical.documentation/regulatory_framework
   technical.documentation/asset_modes
   technical.documentation/uncertainty_modelling

.. toctree::
   :maxdepth: 2
   :caption: API reference

   api/pvbess_opt

.. toctree::
   :maxdepth: 1
   :caption: Project info

   changelog
   license

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
