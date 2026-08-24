"""Spidey Sense web dashboard package (Module 7).

Flask + Jinja2 + vanilla-JS real-time view over the paper-trading ledger.
Public entry points (``BotRuntime``, ``create_app``, ``gate_orchestrator``,
``seed_demo_data``) live in :mod:`dashboard.app`; nothing is re-exported
here so importing the package stays dependency-light.
"""