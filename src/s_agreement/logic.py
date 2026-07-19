"""Minimal agreement domain used to prove Core's application boundary.

An agreement is a shared topic whose children are sections and whose
grandchildren are clauses. Negotiation policy, expiry, and sign-off are
intentionally outside this R7 conformance application.
"""

from __future__ import annotations

from sovereign import ApplicationRegistration, ProtocolNode, Session, SessionResult


AGREEMENT_APPLICATION_ID = "agreement"
AGREEMENT_APP_NAME = "S-Agreement"


class AgreementLogic:
    def __init__(self, session: Session, config: dict | None = None,
                 channel_manager=None):
        self.session = session
        self.config = config or {}
        self.channel_manager = channel_manager
        self.session.identity

    def application_registration(self) -> ApplicationRegistration:
        return ApplicationRegistration(
            AGREEMENT_APPLICATION_ID,
            frozenset({"agreement"}),
            self.agreements,
            self.accept_agreement_invitation,
            assignment_scoped=True,
            mount_invitation=True,
        )

    def agreements(self) -> list[ProtocolNode]:
        found = [
            child for child in self._agreement_container().live_children()
            if child.data.get("type") == "agreement"
        ]
        return sorted(found, key=lambda node: (
            str(node.data.get("title", "")), node.created_at,
        ))

    def create_agreement(self, title: str) -> SessionResult:
        normalized = str(title or "").strip()
        if not normalized:
            return SessionResult("error", reason="agreement title is required")
        result = self.session.create_child(
            self._agreement_container().uuid,
            {"type": "agreement", "title": normalized},
            {},
        )
        if result.status == "ok":
            self._remember_agreement(result.value.uuid)
            return SessionResult(
                "ok", value=result.value.uuid, effects=result.effects,
            )
        return result

    def select_agreement(self, agreement_uuid: str) -> SessionResult:
        agreement = self._node(agreement_uuid, "agreement")
        if not agreement:
            return SessionResult("error", reason="agreement not found")
        self._remember_agreement(agreement.uuid)
        return SessionResult("ok", value=agreement.uuid)

    def create_section(self, agreement_uuid: str, title: str) -> SessionResult:
        agreement = self._node(agreement_uuid, "agreement")
        if not agreement:
            return SessionResult("error", reason="agreement not found")
        normalized = str(title or "").strip()
        if not normalized:
            return SessionResult("error", reason="section title is required")
        result = self.session.create_child(
            agreement.uuid,
            {"type": "agreement_section", "title": normalized},
            {},
        )
        if result.status == "ok":
            return SessionResult(
                "ok", value=result.value.uuid, effects=result.effects,
            )
        return result

    def create_clause(self, section_uuid: str, text: str) -> SessionResult:
        section = self._node(section_uuid, "agreement_section")
        if not section:
            return SessionResult("error", reason="section not found")
        normalized = str(text or "").strip()
        if not normalized:
            return SessionResult("error", reason="clause text is required")
        result = self.session.create_child(
            section.uuid,
            {"type": "agreement_clause", "text": normalized},
            {},
        )
        if result.status == "ok":
            return SessionResult(
                "ok", value=result.value.uuid, effects=result.effects,
            )
        return result

    def update_clause(self, clause_uuid: str, text: str) -> SessionResult:
        clause = self._node(clause_uuid, "agreement_clause")
        if not clause:
            return SessionResult("error", reason="clause not found")
        normalized = str(text or "").strip()
        if not normalized:
            return SessionResult("error", reason="clause text is required")
        data = dict(clause.data)
        data["text"] = normalized
        return self.session.modify(clause.uuid, data, clause.weights)

    def accept_agreement_invitation(self, subtree: ProtocolNode) -> SessionResult:
        if subtree.data.get("type") != "agreement":
            return SessionResult("error", reason="invited topic is not an agreement")
        result = self.session.accept_topic_invitation(
            subtree, self._agreement_container().uuid,
        )
        if result.status == "ok":
            self._remember_agreement(result.value)
        return result

    def adopt_peer_changes(self, source_addr: str,
                           agreement_uuid: str) -> SessionResult:
        if not self._node(agreement_uuid, "agreement"):
            return SessionResult("error", reason="agreement not found")
        changed = self.session.reconcile_peer_changes(
            source_addr, agreement_uuid,
        )
        return SessionResult(
            "ok",
            value=changed,
            effects=self.session.sync_effects(agreement_uuid) if changed else [],
        )

    def transition_events(self, agreement_uuid: str) -> list[dict]:
        events: list[dict] = []
        for address in sorted(self.session.peer_perspectives):
            if not self.session.peer_discusses_node(address, agreement_uuid):
                continue
            events.extend(
                self.session.analyze_peer_transitions(address, agreement_uuid)
            )
        return events

    def document_payload(self, agreement_uuid: str | None = None) -> dict:
        agreements = self.agreements()
        selected = self._selected_agreement(agreement_uuid, agreements)
        events = self.transition_events(selected.uuid) if selected else []
        return {
            "address": self.session.address,
            "agreement": selected.to_dict() if selected else None,
            "agreements": [node.to_dict() for node in agreements],
            "transition_events": events,
            "transition_by_node": self._transition_by_node(events),
            "network": self._network_info(selected.uuid if selected else None),
            # Named mailbox targets and this agreement's assignment. The
            # channel owns both; the application only forwards them so the UI
            # can offer sharing without naming a channel implementation.
            "channel_targets": (
                self.channel_manager.list_targets() if self.channel_manager else []
            ),
            "channel_target_id": (
                self.channel_manager.target_for_topic(selected.uuid)
                if self.channel_manager and selected else None
            ),
        }

    def _selected_agreement(self, requested_uuid: str | None,
                            agreements: list[ProtocolNode]) -> ProtocolNode | None:
        selected_uuid = requested_uuid or self._metadata().get("selected_agreement_uuid")
        selected = self._node(selected_uuid, "agreement") if selected_uuid else None
        if selected:
            return selected
        if agreements:
            self._remember_agreement(agreements[0].uuid)
            return agreements[0]
        return None

    @staticmethod
    def _transition_by_node(events: list[dict]) -> dict:
        priority = {
            "divergence": 5,
            "peer_made_changes": 4,
            "local_missing_node": 4,
            "local_made_changes": 3,
            "peer_missing_node": 3,
            "in_transition": 1,
            "in_agreement": 0,
        }
        grouped: dict[str, dict] = {}
        for event in events:
            node_uuid = event.get("node_uuid")
            if not node_uuid:
                continue
            current = grouped.get(node_uuid)
            if not current or priority.get(event.get("type"), 0) > priority.get(
                    current.get("type"), 0):
                grouped[node_uuid] = dict(event)
        return grouped

    def _network_info(self, topic_uuid: str | None) -> dict:
        if self.channel_manager:
            return self.channel_manager.network_info(topic_uuid)
        return self.session.get_network_info()

    def _node(self, node_uuid: str | None,
              node_type: str) -> ProtocolNode | None:
        node = self.session.protocol.index.get(node_uuid) if node_uuid else None
        return node if node and node.data.get("type") == node_type else None

    def _metadata(self) -> dict:
        apps = self.session.app_metadata.setdefault("apps", {})
        return apps.setdefault(AGREEMENT_APPLICATION_ID, {})

    def _remember_agreement(self, agreement_uuid: str) -> None:
        self._metadata()["selected_agreement_uuid"] = agreement_uuid

    def _agreement_container(self) -> ProtocolNode:
        return self._folder(
            self._apps_folder(), AGREEMENT_APP_NAME, "agreement_app",
        )

    def _apps_folder(self) -> ProtocolNode:
        return self._folder(self.session.protocol.root, "apps")

    def _folder(self, parent: ProtocolNode, name: str,
                node_type: str = "folder") -> ProtocolNode:
        for child in parent.children:
            if (child.data.get("name") == name
                    and child.data.get("type") in ("folder", node_type)):
                return child
        return self.session.create_child(
            parent.uuid, {"type": node_type, "name": name}, {},
        ).value


def create_logic(session: Session, config: dict) -> AgreementLogic:
    logic = AgreementLogic(session, config)
    session.register_application(logic.application_registration())
    return logic
