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
                 collaboration=None):
        self.session = session
        self.config = config or {}
        self.collaboration = collaboration
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

    def sections(self, agreement: ProtocolNode) -> list[ProtocolNode]:
        return self._ordered(agreement, "agreement_section")

    def clauses(self, section: ProtocolNode) -> list[ProtocolNode]:
        return self._ordered(section, "agreement_clause")

    @staticmethod
    def _ordered(parent: ProtocolNode, node_type: str) -> list[ProtocolNode]:
        return sorted(
            [
                child for child in parent.live_children()
                if child.data.get("type") == node_type
            ],
            key=lambda node: (float(node.data.get("order", 0)), node.created_at),
        )

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
            {
                "type": "agreement_section",
                "title": normalized,
                "order": self.session.next_child_order(
                    agreement.uuid, "agreement_section",
                ),
            },
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
            {
                "type": "agreement_clause",
                "text": normalized,
                "order": self.session.next_child_order(
                    section.uuid, "agreement_clause",
                ),
            },
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

    def rename_agreement(self, agreement_uuid: str, title: str) -> SessionResult:
        return self._retitle(agreement_uuid, "agreement", "title", title)

    def rename_section(self, section_uuid: str, title: str) -> SessionResult:
        return self._retitle(section_uuid, "agreement_section", "title", title)

    def _retitle(self, node_uuid: str, node_type: str, field: str,
                 value: str) -> SessionResult:
        node = self._node(node_uuid, node_type)
        if not node:
            return SessionResult("error", reason=f"{node_type} not found")
        normalized = str(value or "").strip()
        if not normalized:
            return SessionResult("error", reason=f"{field} is required")
        data = dict(node.data)
        data[field] = normalized
        return self.session.modify(node.uuid, data, node.weights)

    def delete_agreement(self, agreement_uuid: str) -> SessionResult:
        # An agreement is a topic, so deleting it also stops sharing it -
        # otherwise peers keep syncing a document this side no longer has.
        # There is no "last agreement" to protect: unlike a board, nothing
        # here creates one on demand, and a host with none is a valid state.
        agreement = self._node(agreement_uuid, "agreement")
        if not agreement:
            return SessionResult("error", reason="agreement not found")
        release = self.session.end_topic_sharing(agreement.uuid)
        result = self.session.delete(agreement.uuid)
        if result.status != "ok":
            return result
        result.effects = [*release.effects, *result.effects]
        remaining = [item for item in self.agreements() if item.uuid != agreement.uuid]
        self._remember_agreement(remaining[0].uuid if remaining else "")
        return result

    def delete_section(self, section_uuid: str) -> SessionResult:
        # Deleting a section takes its clauses with it. That is safe here
        # only because the request is local and explicit; adopting a peer's
        # section deletion is a separate decision this application still
        # leaves to the generic reconciliation path.
        section = self._node(section_uuid, "agreement_section")
        if not section:
            return SessionResult("error", reason="section not found")
        return self.session.delete(section.uuid)

    def delete_clause(self, clause_uuid: str) -> SessionResult:
        clause = self._node(clause_uuid, "agreement_clause")
        if not clause:
            return SessionResult("error", reason="clause not found")
        return self.session.delete(clause.uuid)

    def move_section(self, section_uuid: str, index: int) -> SessionResult:
        if not self._node(section_uuid, "agreement_section"):
            return SessionResult("error", reason="section not found")
        return self.session.move_child_to_index(section_uuid, index)

    def move_clause(self, clause_uuid: str, index: int) -> SessionResult:
        if not self._node(clause_uuid, "agreement_clause"):
            return SessionResult("error", reason="clause not found")
        return self.session.move_child_to_index(clause_uuid, index)

    def accept_agreement_invitation(self, subtree: ProtocolNode) -> SessionResult:
        if subtree.data.get("type") != "agreement":
            return SessionResult("error", reason="invited topic is not an agreement")
        result = self.session.accept_topic_invitation(
            subtree, self._agreement_container().uuid,
        )
        if result.status == "ok":
            self._remember_agreement(result.value)
        return result

    # Reacting per node is what lets a divergence be left behind. Without it
    # an agreement can reach a state it cannot exit: two sides edit the same
    # clause, both see "diverged", and nothing either of them does resolves
    # it. Both primitives are Session's; this application only names which
    # node types may be reacted to.
    REACTABLE = frozenset({"agreement", "agreement_section", "agreement_clause"})

    def accept_peer_node(self, source_addr: str, node_uuid: str,
                         adopt_absence: bool = False) -> SessionResult:
        if not self._reactable(node_uuid):
            return SessionResult("error", reason="node is not part of an agreement")
        return self.session.accept_peer_node(source_addr, node_uuid, adopt_absence)

    def rollback_peer_node(self, source_addr: str, node_uuid: str,
                           rollback_absence: bool = False) -> SessionResult:
        if not self._reactable(node_uuid):
            return SessionResult("error", reason="node is not part of an agreement")
        return self.session.rollback_peer_node(
            source_addr, node_uuid, rollback_absence,
        )

    def _reactable(self, node_uuid: str) -> bool:
        # A node absent locally is exactly the case worth reacting to - the
        # peer has something this side does not - so absence is permitted and
        # only a present node of a foreign type is refused.
        node = self.session.protocol.index.get(node_uuid)
        return node is None or node.data.get("type") in self.REACTABLE

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

    def transition_events(
        self, agreement_uuid: str, network: dict | None = None,
    ) -> list[dict]:
        events: list[dict] = []
        for address in sorted(self.session.peer_perspectives):
            if not self.session.peer_discusses_node(address, agreement_uuid):
                continue
            peer_info = ((network or {}).get("peers") or {}).get(address) or {}
            liveness = peer_info.get("channel_liveness")
            if liveness is None:
                resolver = getattr(
                    self.collaboration, "peer_liveness_for_address", None,
                )
                liveness = (
                    resolver(address, agreement_uuid)
                    if resolver else {"state": "unknown"}
                )
            liveness = liveness or {"state": "unknown"}
            events.extend(
                event
                for event in self.session.analyze_peer_transitions(
                    address, agreement_uuid,
                )
                if not (
                    event["type"] == "in_transition"
                    and liveness.get("state") == "stale"
                )
            )
        return events

    def document_payload(self, agreement_uuid: str | None = None) -> dict:
        agreements = self.agreements()
        selected = self._selected_agreement(agreement_uuid, agreements)
        network = self._network_info(selected.uuid if selected else None)
        events = (
            self.transition_events(selected.uuid, network) if selected else []
        )
        return {
            "address": self.session.address,
            "agreement": selected.to_dict() if selected else None,
            "agreements": [node.to_dict() for node in agreements],
            "transition_events": events,
            "transition_by_node": self._transition_by_node(events),
            # Agreement changes are proposals until explicitly accepted.
            # Expose only the peer-only agreement nodes needed to present
            # those proposals; the application does not receive or manage
            # channel state, nor does the UI need the complete peer cache.
            "proposed_nodes": self._proposed_nodes(events),
            "network": network,
            # Agendas are Session's, so this application only forwards the
            # merged list for the topic in view.
            "agenda_items": [
                node.to_dict() for node in
                (self.session.agenda_items(selected.uuid) if selected else [])
            ],
            "identity_uuid": self.session.identity.uuid,
            "known_identities": self.session.known_identities(),
        }

    def _proposed_nodes(self, events: list[dict]) -> list[dict]:
        proposals: list[dict] = []
        seen: set[str] = set()
        for event in events:
            if event.get("type") != "local_missing_node":
                continue
            node_uuid = event.get("node_uuid")
            source_addr = event.get("peer_addr")
            if not node_uuid or not source_addr or node_uuid in seen:
                continue
            peer_node = self.session.get_cached_peer_subtree(
                source_addr, node_uuid,
            )
            if (
                not peer_node
                or peer_node.deleted
                or peer_node.data.get("type") not in self.REACTABLE
            ):
                continue
            seen.add(node_uuid)
            proposals.append({
                "source_addr": source_addr,
                "node": peer_node.to_dict(),
            })
        return proposals

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

    def _transition_by_node(self, events: list[dict]) -> dict:
        priority = Session.TRANSITION_PRIORITY
        grouped: dict[str, dict] = {}
        for event in events:
            node_uuid = event.get("node_uuid")
            if not node_uuid:
                continue
            current = grouped.get(node_uuid)
            if not current or priority.get(event.get("type"), 0) > priority.get(
                    current.get("type"), 0):
                # The reaction rides with the transition so the view never has
                # to work out whether this side or the peer holds the stale
                # revision.
                grouped[node_uuid] = dict(
                    event, reaction=self.session.reaction_for_event(event),
                )
        return grouped

    def _network_info(self, topic_uuid: str | None) -> dict:
        if self.collaboration:
            return self.collaboration.network_info(topic_uuid)
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
