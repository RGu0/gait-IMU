"""L1 device layer — talks to the hardware, and nothing else.

Modules, and the Issue that delivers each::

    protocol.py    F1.6 0x55 streaming parser, FF AA command framing
                   -> RAY-195
    ble.py         F1.1-1.3 scan, dual-device concurrent connect, config push
                   -> RAY-196, RAY-197
    recorder.py    F1.4 raw frames to disk as the receive callback's first action
                   -> RAY-198

Pure-function protocol parsing stays in protocol.py so it can be unit
tested without a radio.

**ble.py and recorder.py currently hold RAY-200's minimal subset, not the
full RAY-196/197/198 delivery.** ble.py has `configure_streaming` (the fixed
PRD §6.1 write sequence) but not scan/connect/MAC-binding orchestration —
`cli/linktest.py` does that inline for now, to be absorbed here when
RAY-196/197 land. recorder.py has the threaded disk writer but not session
directory layout or crash recovery.

"""
