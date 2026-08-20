"""L8 validation layer.

Modules, and the Issue that delivers each::

    metrics.py     F6.3 RMSE / MAPE / Bland-Altman / ICC
                   -> RAY-194
    protocols.py   F6.3 institution-facing protocols, e.g. the 4 m round trip
                   -> RAY-230
    synthetic.py   F6.4 synthetic gait data generator
                   -> RAY-206

synthetic.py is what lets algorithm work proceed without hardware; the
end-to-end regression over it (V1-a) is the most important test in the
repository.

"""
