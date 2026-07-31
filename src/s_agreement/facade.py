"""Versioned public query/command facade exposed by S-Agreement.

Mirrors S-Kanban's facade: a stable, explicitly versioned surface an
aggregator like Personal Cockpit can consume without importing this package.
"""

from __future__ import annotations

from sovereign import ProtocolNode

from .logic import AgreementLogic


AGREEMENT_FACADE_API_VERSION = 1


class AgreementFacade:
    """Stable facade returning detached node snapshots and command results."""

    def __init__(self, logic: AgreementLogic):
        self._logic = logic

    def agreements(self) -> list[ProtocolNode]:
        return self._logic.agreements()

    def sections(self, agreement: ProtocolNode) -> list[ProtocolNode]:
        return self._logic.sections(agreement)

    def clauses(self, section: ProtocolNode) -> list[ProtocolNode]:
        return self._logic.clauses(section)

    def subagreement_links(
        self, agreement: ProtocolNode,
    ) -> list[ProtocolNode]:
        return self._logic.subagreement_links(agreement)

    def roles(self, agreement: ProtocolNode) -> list[ProtocolNode]:
        return self._logic.roles(agreement)

    def accountabilities(self, role: ProtocolNode) -> list[ProtocolNode]:
        return self._logic.accountabilities(role)

    def domains(self, role: ProtocolNode) -> list[ProtocolNode]:
        return self._logic.domains(role)

    def organization(self) -> dict:
        return self._logic.organization_payload()

    def acceptance_badges(self, agreement_uuid: str) -> list[dict]:
        return self._logic.acceptance_badges(agreement_uuid)

    def transition_events(
        self, agreement_uuid: str, network: dict | None = None,
    ) -> list[dict]:
        return self._logic.transition_events(agreement_uuid, network)

    def transition_by_node(self, events: list[dict]) -> dict:
        return self._logic.transition_by_node(events)

    def collaboration_context(
        self, topic_uuid: str, network: dict | None = None,
    ) -> dict:
        return self._logic.collaboration_context(topic_uuid, network)

    def create_agreement(self, title: str):
        return self._logic.create_agreement(title)

    def create_subagreement(self, parent_agreement_uuid: str, title: str):
        return self._logic.create_subagreement(parent_agreement_uuid, title)

    def set_decision(
        self,
        agreement_uuid: str,
        decision: str,
        expires_at: str | None = None,
    ):
        return self._logic.set_decision(
            agreement_uuid, decision, expires_at,
        )

    def delete_agreement(self, agreement_uuid: str):
        return self._logic.delete_agreement(agreement_uuid)

    def create_agenda_item(
        self, agreement_uuid: str, text: str, priority: str | None = None,
    ):
        return self._logic.create_agenda_item(
            agreement_uuid, text, priority,
        )

    def delete_agenda_item(self, item_uuid: str):
        return self._logic.delete_agenda_item(item_uuid)

    def set_agenda_item_priority(
        self, item_uuid: str, priority: str | None,
    ):
        return self._logic.set_agenda_item_priority(item_uuid, priority)

    def move_agenda_item(self, item_uuid: str, index: int):
        return self._logic.move_agenda_item(item_uuid, index)
