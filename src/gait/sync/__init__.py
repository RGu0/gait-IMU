"""L4 synchronisation layer.

Modules, and the Issue that delivers each::

    timebase.py    F3.2 host receive-time base: min-filter + linear regression
                   -> RAY-209
    integrity.py   F3.3/3.5 arrival-rate monitoring and data-gap splitting
                   -> RAY-210
    selfcheck.py   F3.4 synchronisation quality self-check
                   -> RAY-211
    anchor.py      F3.1 physical tap anchor — engineering mode only
                   -> RAY-212

PRD v1.2 §8/§14: the host receive-time model is the default time base.
anchor.py is demoted to a lab ground-truth tool for V3' and is off by
default.

"""
