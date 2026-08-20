"""L6 gait analysis layer — pure NavResult -> list[GaitCycle] transforms.

Modules, and the Issue that delivers each::

    segments.py    straight/turn separation and mid-segment step selection
                   -> RAY-215
    events.py      F5.1 gait event segmentation
                   -> RAY-216
    temporal.py    F5.3 temporal parameters
                   -> RAY-216
    spatial.py     F5.2/5.6 spatial parameters and trajectory
                   -> RAY-216
    velocity.py    F5.4 velocity parameters
                   -> RAY-216
    symmetry.py    F5.5 symmetry, CV, fatigue decay, turning metrics
                   -> RAY-217

"""
