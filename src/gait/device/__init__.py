"""L1 device layer — talks to the hardware, and nothing else.

Modules, and the Issue that delivers each::

    adapter.py     F1.6 wt901 ImuSample -> contract RawFrame, accel saturation
                   -> RAY-195
    binding.py     F1.1-1.2 left/right foot binding, persistence, session
                   admission, re-pairing
                   -> RAY-196
    ble.py         F1.3 the fixed config sequence and its readback checks
                   -> RAY-197
    capture.py     F1.4 session-level record orchestration, replay bridge and
                   crash-truncation recovery
                   -> RAY-198
    identity.py    F1.1 device-reported MAC as the binding key, with the
                   derivation recorded alongside it
                   -> RAY-302
    orchestration.py
                   F1.3 session admission (battery gate), link outcome and
                   session completeness, closing telemetry
                   -> RAY-197
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

**binding.py does not read device identity; it consumes one.** That injection
point is now filled by `identity.py` (WT901 RAY-279 landed `read_mac()`), but
the seam stays: binding logic must not depend on where the key came from.

Each stored identity carries two things beyond its value. `kind` distinguishes
*which* identity (mac / serial / platform-address) -- a later change of source
would otherwise be indistinguishable from "this is a different device".
`provenance` distinguishes *which derivation within that kind*, because the MAC
byte layout is **inferred, not externally confirmed**: if it is ever overturned,
the same device yields a different value under the same `kind`. See
`identity.py` for the evidence and for how to record a confirmation without
invalidating existing bindings.

**ble.py and recorder.py currently hold RAY-200's minimal subset, not the full
RAY-197/198 delivery.** ble.py has `configure_streaming` (the fixed PRD §6.1
write sequence) but not connect orchestration -- `cli/linktest.py` does that
inline for now, to be absorbed here when RAY-197 lands. recorder.py has the
threaded disk writer but not session directory layout or crash recovery.

"""
