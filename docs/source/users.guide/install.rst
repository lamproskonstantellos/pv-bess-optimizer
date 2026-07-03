Installation
============

Pure-Python; runs on Linux, macOS, Windows with Python ≥ 3.11.

.. code-block:: bash

   git clone https://github.com/lamproskonstantellos/pv-bess-optimizer
   cd pv-bess-optimizer
   pip install -r requirements/dev.txt   # base + solvers + linters + pytest

Solvers
-------

The default solver is HiGHS, installed via ``highspy``.  The optimiser
falls back through (user-specified, HiGHS, CBC) on import failure.

* **HiGHS**: open-source, installed via ``pip install highspy``.
* **Gurobi**: commercial; install ``gurobipy`` + a valid licence.
* **CBC**: open-source; install via the OS package manager
  (``brew install cbc``, ``sudo apt install coinor-cbc``).
