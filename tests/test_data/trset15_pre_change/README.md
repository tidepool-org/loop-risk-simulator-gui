# TRSET-15 pre-change baseline (AC 4)

Generated on 2026-09-04, before any TRSET-15 code existed, by `capture_baseline.py`
on unmodified `main` at commit 1cd3ab564c90ed8a31dba98be66ddcd4b1954537.

That script has since been removed. It is not recoverable output: it could only ever
produce this baseline when run against that commit, and once `meal_config.py` changed
there was no flag that would make the new generator emit the old shape. These files
are the record; the commit hash above is where they came from.

Spec: standard mode, one meal at 2019-08-15 13:00, 8-hour duration,
risk id TLR-20260904-120000, no risk description.

AC 4 asserts that TRSET-15's default selection (Loop 2.x + Autobolus) still
produces these files, apart from the two new metadata keys. If that assertion
ever fails, the default config-generation path changed — that is a finding to
investigate, not a fixture to regenerate.