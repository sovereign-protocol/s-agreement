"""Versioned public query facade exposed by S-Agreement.

Mirrors S-Kanban's facade: a stable, explicitly versioned surface an
aggregator like Personal Cockpit can consume without importing this package.
"""

from __future__ import annotations

from sovereign import ProtocolNode, Session

from .logic import AgreementLogic


AGREEMENT_FACADE_API_VERSION = 1


class AgreementFacade:
    """Stable query surface for optional cross-application consumers."""

    def __init__(self, logic: AgreementLogic):
        self._logic = logic

    def agreements(self) -> list[ProtocolNode]:
        return self._logic.agreements()

    def session(self) -> Session:
        return self._logic.session

    def transition_events(self, agreement_uuid: str) -> list[dict]:
        return self._logic.transition_events(agreement_uuid)

    def transition_by_node(self, events: list[dict]) -> dict:
        return self._logic._transition_by_node(events)
