Installation
============

Pure-Python; runs on Linux, macOS, Windows with Python ≥ 3.11.

.. code-block:: bash

   git clone https://github.com/lamproskonstantellos/pv-bess-optimizer
   cd pv-bess-optimizer
   pip install -r requirements/dev.txt   # base + solvers + linters + pytest
   pip install -e .                      # optional: the ``pvbess`` console command

Every documented ``pvbess ...`` invocation also works as
``python main.py ...`` from the repository root without the editable
install.

Solvers
-------

The default solver is HiGHS, installed via ``highspy``.  The requested
solver either runs or the run stops with an error listing the solvers
that ARE installed — it is never substituted silently, because the
solver is part of the results' provenance (the ``[verify] solver=``
run-log line and the SUMMARY header).

* **HiGHS**: open-source, installed via ``pip install highspy``.
* **Gurobi**: commercial; install ``gurobipy`` + a valid licence.
* **CBC**: open-source; install via the OS package manager
  (``brew install cbc``, ``sudo apt install coinor-cbc``).
