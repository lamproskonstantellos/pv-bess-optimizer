# Reference data

## `pv_shape_15min.csv`

Canonical PV shape από real-world 8 MW site (annual yield 12 568 961,75 kWh,
specific production 1 571,12 kWh/kWp).  Χρησιμοποιείται **μόνο** ως normalized
shape — η annual energy κλιμακώνεται από το workbook input.

* **Rows:** 35 040 (full year @ 15-minute cadence).
* **Header:** `pv_kwh_8mw_reference`.
* **Units:** kWh per 15-minute step, in the original 8 MW scale.
* **Non-negative** by construction; ~18 060 zeros (nights / dawn-dusk).
* **Loaded by:** `scripts/build_input_xlsx.py::generate_pv_timeseries`.

The loader normalises the column to unit-sum and multiplies by the user's
`pv_nameplate_kwp × specific_production_kwh_per_kwp` to produce a per-step
kWh series whose annual total exactly matches the workbook intent.  No
random numbers, no smoothing, no noise — same inputs ⇒ identical
bit-exact output.
