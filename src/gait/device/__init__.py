"""L1 device layer — talks to the hardware, and nothing else.

Modules, and the Issue that delivers each::

    adapter.py     F1.6 wt901 ImuSample -> contract RawFrame, accel saturation
                   -> RAY-195
    ble.py         F1.1-1.3 scan, dual-device concurrent connect, config push
                   -> RAY-196, RAY-197
    recorder.py    F1.4 raw frames to disk as the receive callback's first action
                   -> RAY-198

**There is no protocol.py, and there should not be one.** RAY-193's R2 adopted
wt901 for the hardware interface, so 0x55 frame decoding, unit conversion, the
register table and FF AA command framing all come from the dependency -- see
the rationale committed in pyproject.toml. RAY-195 was written before that
decision and its R2 narrowed it accordingly: what stays here is only what
wt901 deliberately does not do, namely the saturation judgement and the
contract boundary. Reaching for a hand-written parser here means duplicating a
pinned dependency.

**ble.py and recorder.py currently hold RAY-200's minimal subset, not the
full RAY-196/197/198 delivery.** ble.py has `configure_streaming` (the fixed
PRD §6.1 write sequence) but not scan/connect/MAC-binding orchestration --
`cli/linktest.py` does that inline for now, to be absorbed here when
RAY-196/197 land. recorder.py has the threaded disk writer but not session
directory layout or crash recovery.

"""
