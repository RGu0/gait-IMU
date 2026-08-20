"""Unified configuration system.

Every layer may read this module; it depends on no other module in the
package. That asymmetry is deliberate — it is what keeps configuration from
becoming a back door through the layering.

Contents are delivered by RAY-193 together with the session data format and
metadata schema, and include the test-duration presets (60 / 120 / 180 s,
default 180) that PRD v1.2 §7 requires to be configurable.

This scope (RAY-192 ``package-skeleton``) only establishes the module so the
layer map is complete; it deliberately defines no configuration keys, because
inventing them here would pre-empt the schema RAY-193 has to freeze.
"""
