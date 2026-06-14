"""worldcup-kalshi-edge.

In-play 2026 FIFA World Cup probability model + Kalshi market edge detection,
risk sizing, and execution that defaults to safe local simulation.

The package is organised as an explicit, swappable pipeline:

    ingestion -> features -> modeling -> market(implied) -> edge
              -> risk(sizing+guardrails) -> execution -> observability

Every stage hides behind an interface so it can be tested and replaced in isolation.
"""

__version__ = "0.1.0"
