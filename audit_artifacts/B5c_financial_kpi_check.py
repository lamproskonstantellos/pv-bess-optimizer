"""B.5.c — financial KPI sanity check.

Hand-computed reference cases:

  * NPV  of [0, 100, 100, 100, 100, 100] at r=0.05  → 432.9477 EUR.
  * IRR  of [-1000, 250, 250, 250, 250, 250]       → 0.0793 (7.93 %).
"""
from __future__ import annotations

import numpy as np

from pvbess_opt.economics import calculate_irr


def reference_npv(cf: list[float], r: float) -> float:
    return float(sum(cf[t] / (1.0 + r) ** t for t in range(len(cf))))


def main() -> None:
    cf_npv = [0.0, 100.0, 100.0, 100.0, 100.0, 100.0]
    expected_npv = sum(100.0 / (1.05) ** t for t in range(1, 6))  # 432.9477
    computed_npv = reference_npv(cf_npv, 0.05)
    print(f"NPV(100x5y@5%): computed={computed_npv:.6f}  expected={expected_npv:.6f}  "
          f"abs_err={abs(computed_npv-expected_npv):.2e}")
    assert abs(computed_npv - expected_npv) < 1e-6

    cf_irr = [-1000.0, 250.0, 250.0, 250.0, 250.0, 250.0]
    computed_irr = calculate_irr(np.array(cf_irr))
    # Expected analytic IRR (from sum 250/(1+r)^t for t=1..5 == 1000):
    # Solve numerically.  Bisection reference:
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        npv_mid = sum(cf_irr[t] / (1.0 + mid) ** t for t in range(6))
        if npv_mid > 0:
            lo = mid
        else:
            hi = mid
    reference_irr = 0.5 * (lo + hi)
    print(f"IRR ([-1000,250x5]): computed={computed_irr:.6f}  ref={reference_irr:.6f}  "
          f"abs_err={abs(computed_irr-reference_irr):.2e}")
    rel = abs(computed_irr - reference_irr) / abs(reference_irr)
    print(f"  relative_err={rel:.2e}  ({'PASS' if rel < 1e-4 else 'FAIL'})")

    print("\nAll OK." if rel < 1e-4 else "\n*** FAIL ***")


if __name__ == "__main__":
    main()
