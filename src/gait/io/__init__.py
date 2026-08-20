"""L2 data layer — reading, writing and format conversion only.

Modules, and the Issue that delivers each::

    session.py     F0.2 session directory and metadata read/write
                   -> RAY-193
    rawlog.py      raw frame file read/write and replay
                   -> RAY-198
    export.py      F6.1 CSV / HDF5 export
                   -> RAY-193

The package name shadows the standard library's ``io`` only as an
attribute of ``gait``; absolute imports mean ``import io`` inside this
package still reaches the standard library.

"""
