"""Command Voice Bridge.

An agentic service that binds team voice channels to a live game session:
it detects the active match, splits players into two isolated voice channels
(Radiant / Dire), applies mutes, and drives a transparent overlay.
"""

__version__ = "0.1.0"
