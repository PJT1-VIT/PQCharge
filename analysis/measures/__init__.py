"""
The MEASURE step: one module per experiment, each turning a matched run
(or, for E4 and the external-node table, the whole diary) into numbers.

Every measure returns a plain dict ready for results.json, or None when the
run holds no data for that experiment (a run with no server restart has no
E2 result -- None, never a zero that looks like a measurement).
"""
