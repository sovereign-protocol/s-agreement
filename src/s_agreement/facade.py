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

    def parent_holdings(self, agreement: ProtocolNode) -> list[ProtocolNode]:
        return self._logic.parent_holdings(agreement)

    def child_agreements(
        self, agreement: ProtocolNode,
    ) -> list[tuple[str, ProtocolNode]]:
        return self._logic.child_agreements(agreement)

    def identity_holder(self, agreement: ProtocolNode) -> str:
        return self._logic.identity_holder(agreement)

    def identity(self, agreement: ProtocolNode) -> dict:
        return self._logic.identity_payload(agreement)

    def take_identity(self, agreement_uuid: str):
        return self._logic.take_identity(agreement_uuid)

    def offer_identity(self, agreement_uuid: str, actor_uuid: str):
        return self._logic.offer_identity(agreement_uuid, actor_uuid)

    def roles(self, agreement: ProtocolNode) -> list[ProtocolNode]:
        return self._logic.roles(agreement)

    def accountabilities(self, role: ProtocolNode) -> list[ProtocolNode]:
        return self._logic.accountabilities(role)

    def domains(self, role: ProtocolNode) -> list[ProtocolNode]:
        return self._logic.domains(role)

    def role_offers(self, role: ProtocolNode) -> list[ProtocolNode]:
        return self._logic.role_offers(role)

    def role_holders(
        self, agreement: ProtocolNode, role: ProtocolNode,
    ) -> list[dict]:
        return self._logic.role_holders(agreement, role)

    def offer_role(self, role_uuid: str, actor_uuid: str):
        return self._logic.offer_role(role_uuid, actor_uuid)

    def revoke_role_offer(self, role_uuid: str, actor_uuid: str):
        return self._logic.revoke_role_offer(role_uuid, actor_uuid)

    def decide_role(
        self, role_uuid: str, decision: str, expires_at: str | None = None,
    ):
        return self._logic.decide_role(role_uuid, decision, expires_at)

    def resign_role(self, role_uuid: str):
        return self._logic.resign_role(role_uuid)

    def seat_agreement(self, role_uuid: str, agreement_uuid: str):
        return self._logic.seat_agreement(role_uuid, agreement_uuid)

    def decline_seat(self, role_uuid: str, agreement_uuid: str):
        return self._logic.decline_seat(role_uuid, agreement_uuid)

    def unseat_agreement(self, role_uuid: str, agreement_uuid: str):
        return self._logic.unseat_agreement(role_uuid, agreement_uuid)

    def seat_offers(self, agreement: ProtocolNode) -> list[dict]:
        return self._logic.seat_offers(agreement)

    def create_seated_agreement(self, role_uuid: str, title: str):
        return self._logic.create_seated_agreement(role_uuid, title)

    def parents(self, agreement: ProtocolNode) -> list[dict]:
        return self._logic.parent_payload(agreement)

    def home_parent_uuid(self, agreement: ProtocolNode) -> str:
        return self._logic.home_parent_uuid(agreement)

    def organization(self) -> dict:
        return self._logic.organization_payload()

    def participants(self, agreement_uuid: str) -> list[dict]:
        return self._logic.participants(agreement_uuid)

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

    def clone_agreement(self, agreement_uuid: str, title: str | None = None):
        return self._logic.clone_agreement(agreement_uuid, title)

    def actor_uuids(self, agreement: ProtocolNode) -> set[str]:
        return self._logic.actor_uuids(agreement)

    def agreement_state(self, agreement: ProtocolNode) -> str:
        return self._logic.agreement_state(agreement)

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
