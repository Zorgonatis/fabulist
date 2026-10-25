"""RP spike harness: scenario-driven roleplay runs against the live engine API.

Modules
-------
* :mod:`scripts.spike.scenario` — strict scenario YAML model + loader.
* :mod:`scripts.spike.client` — :class:`SpikeClient` HTTP/SSE transport.
* :mod:`scripts.spike.player` — :class:`PlayerSim` model-driven co-player.
* :mod:`scripts.spike.run` — CLI entry (:func:`main`).

Ownership: ``scripts/spike/**`` and ``tests/test_spike_*.py`` only.
"""
