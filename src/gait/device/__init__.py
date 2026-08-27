"""L1 device layer — talks to the hardware, and nothing else.

Modules, and the Issue that delivers each::

    adapter.py     F1.6 wt901 ImuSample -> contract RawFrame, accel saturation
                   -> RAY-195
    binding.py     F1.1-1.2 left/right foot binding, persistence, session
                   admission, re-pairing
                   -> RAY-196
    ble.py         F1.3 dual-device concurrent connect, config push
                   -> RAY-197
    capture.py     F1.4 session-level record orchestration and replay bridge
                   -> RAY-198
    recorder.py    F1.4 hot-path-safe threaded writer under capture.py
                   -> RAY-198

**There is no protocol.py, and there should not be one.** RAY-193's R2 adopted
wt901 for the hardware interface, so 0x55 frame decoding, unit conversion, the
register table and FF AA command framing all come from the dependency -- see
the rationale committed in pyproject.toml. RAY-195 was written before that
decision and its R2 narrowed it accordingly: what stays here is only what
wt901 deliberately does not do, namely the saturation judgement and the
contract boundary. Reaching for a hand-written parser here means duplicating a
pinned dependency.

**There is no scan wrapper either.** `wt901.scan` already filters by the `WT`
name substring, sorts by RSSI descending and puts unknown-RSSI devices last,
which is the whole of RAY-196's scan requirement. Wrapping it would add a layer
that only forwards.

**binding.py does not read device identity; it consumes one.** wt901 has no
cross-host-persistable identity yet -- `DiscoveredDevice.address` is a
CoreBluetooth UUID on macOS and its own docstring forbids persisting it. The
manual's answer is register `0x66` (device-reported MAC), which wt901 does not
expose (WT901 RAY-279, blocked on real-device evidence for the response byte
layout). So identity is injected, and each stored identity carries its `kind` --
otherwise a later change of identity source is indistinguishable from "this is a
different device".

**ble.py and recorder.py currently hold RAY-200's minimal subset, not the full
RAY-197/198 delivery.** ble.py has `configure_streaming` (the fixed PRD §6.1
write sequence) but not connect orchestration -- `cli/linktest.py` does that
inline for now, to be absorbed here when RAY-197 lands. recorder.py has the
threaded disk writer but not session directory layout or crash recovery.

"""
