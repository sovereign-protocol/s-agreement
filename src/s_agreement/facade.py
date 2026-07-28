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

    def sections(self, agreement: ProtocolNode) -> list[ProtocolNode]:
        return self._logic.sections(agreement)

    def clauses(self, section: ProtocolNode) -> list[ProtocolNode]:
        return self._logic.clauses(section)

    def session(self) -> Session:
        return self._logic.session

    def transition_events(self, agreement_uuid: str) -> list[dict]:
        return self._logic.transition_events(agreement_uuid)

    def transition_by_node(self, events: list[dict]) -> dict:
        return self._logic._transition_by_node(events)

    def collaboration_context(self, topic_uuid: str) -> dict:
        agreement = self._logic.session.get_node(topic_uuid)
        if not agreement or agreement.data.get("type") != "agreement":
            return {}
        events = self._logic.transition_events(topic_uuid)
        return {
            "agenda_items": [
                item.to_dict() for item in self._logic.session.agenda_items(topic_uuid)
            ],
            "transition_events": events,
            "transition_by_node": self._logic._transition_by_node(events),
            "identity_uuid": self._logic.session.identity.uuid,
            "known_identities": self._logic.session.known_identities(),
        }
