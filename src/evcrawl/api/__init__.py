"""The read-only events API the Usher PWA consumes (``docs/specs/phase-4-api.md``, ADR 0014).

It reads the ``api_*`` views of migration ``0008`` as ``api_ro`` and nothing
else; ``create_app`` in ``app.py`` builds it and ``evcrawl api`` serves it. The
contract the PWA builds against is ``docs/api/`` (``openapi.json`` is generated
from this package and checked by a test).
"""
