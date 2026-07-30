"""Minimal agreement domain used to prove Core's application boundary.

An agreement is a shared topic whose children are sections and whose
grandchildren are clauses. Negotiation policy, expiry, and sign-off are
intentionally outside this R7 conformance application.
"""

from __future__ import annotations

import copy

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
        with self.session.lock:
            self.session.application_metadata(AGREEMENT_APPLICATION_ID)

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
        container = self._find_agreement_container()
        if not container:
            return []
        found = [
            child for child in container.live_children()
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
    OWNED_NODE_TYPES = frozenset({*REACTABLE, "agenda_item"})

    def accept_peer_node(self, source_addr: str, node_uuid: str,
                         adopt_absence: bool = False) -> SessionResult:
        local_exists = node_uuid in self.session.protocol.index
        if ((local_exists and not self.owns_node(node_uuid))
                or (not adopt_absence
                    and not self.owns_node(node_uuid, source_addr))):
            return SessionResult("error", reason="node is not part of an agreement")
        return self.session.accept_peer_node(source_addr, node_uuid, adopt_absence)

    def rollback_peer_node(self, source_addr: str, node_uuid: str,
                           rollback_absence: bool = False) -> SessionResult:
        if (not self.owns_node(node_uuid)
                or (not rollback_absence
                    and not self.owns_node(node_uuid, source_addr))):
            return SessionResult("error", reason="node is not part of an agreement")
        return self.session.rollback_peer_node(
            source_addr, node_uuid, rollback_absence,
        )

    def adopt_peer_changes(self, source_addr: str,
                           agreement_uuid: str) -> SessionResult:
        if (
            not self._node(agreement_uuid, "agreement")
            or not self.owns_node(agreement_uuid, source_addr)
        ):
            return SessionResult("error", reason="agreement not found")
        changed = self.session.reconcile_peer_changes(
            source_addr, agreement_uuid,
        )
        return SessionResult("ok", value=changed)

    def transition_events(
        self, agreement_uuid: str, network: dict | None = None,
    ) -> list[dict]:
        events: list[dict] = []
        for address in self.session.peer_addresses():
            if not self.session.peer_discusses_node(address, agreement_uuid):
                continue
            peer_info = ((network or {}).get("peers") or {}).get(address) or {}
            liveness = peer_info.get("channel_liveness")
            if liveness is None and network is None:
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

    def document_payload(
        self, agreement_uuid: str | None = None,
        network: dict | None = None,
    ) -> dict:
        agreements = self.agreements()
        selected = self._selected_agreement(agreement_uuid, agreements)
        network = (
            self._network_info(selected.uuid if selected else None)
            if network is None else network
        )
        events = (
            self.transition_events(selected.uuid, network) if selected else []
        )
        return {
            "address": self.session.address,
            "agreement": selected.to_dict() if selected else None,
            "agreements": [node.to_dict() for node in agreements],
            "transition_events": events,
            "transition_by_node": self.transition_by_node(events),
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

    def document_snapshot(
        self, agreement_uuid: str | None = None,
    ) -> dict:
        """Build agreement state under Session without consulting transport."""
        payload = self.document_payload(agreement_uuid, {})
        decorated = []
        for event in payload.get("transition_events", []):
            node_uuid = event.get("node_uuid")
            view = self.transition_by_node([event]).get(node_uuid)
            if view:
                decorated.append((event, view))
        agreement = payload.get("agreement") or {}
        return {
            "payload": payload,
            "topic_uuid": agreement.get("uuid"),
            "transition_views": decorated,
        }

    @classmethod
    def merge_document_observation(
        cls, snapshot: dict, network: dict,
    ) -> dict:
        """Decorate a detached agreement snapshot with channel liveness."""
        payload = snapshot["payload"]
        visible_events = []
        grouped = {}
        for event, view in snapshot.get("transition_views", []):
            if not cls._transition_visible(event, network):
                continue
            visible_events.append(event)
            node_uuid = event.get("node_uuid")
            current = grouped.get(node_uuid)
            if (
                current is None
                or Session.TRANSITION_PRIORITY.get(event.get("type"), 0)
                > Session.TRANSITION_PRIORITY.get(current.get("type"), 0)
            ):
                grouped[node_uuid] = view
        payload["network"] = network
        payload["transition_events"] = visible_events
        payload["transition_by_node"] = grouped
        return payload

    @staticmethod
    def _transition_visible(event: dict, network: dict) -> bool:
        if event.get("type") != "in_transition":
            return True
        peer = ((network.get("peers") or {}).get(
            event.get("peer_addr"),
        ) or {})
        state = (peer.get("channel_liveness") or {}).get("state", "unknown")
        return state != "stale"

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
            return agreements[0]
        return None

    def transition_by_node(self, events: list[dict]) -> dict:
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

    def collaboration_context(
        self, topic_uuid: str, network: dict | None = None,
    ) -> dict:
        agreement = self._node(topic_uuid, "agreement")
        if not agreement:
            return {}
        events = self.transition_events(topic_uuid, network)
        return {
            "agenda_items": [
                item.to_dict() for item in self.session.agenda_items(topic_uuid)
            ],
            "transition_events": events,
            "transition_by_node": self.transition_by_node(events),
            "identity_uuid": self.session.identity.uuid,
            "known_identities": self.session.known_identities(),
        }

    def _network_info(self, topic_uuid: str | None) -> dict:
        if self.collaboration:
            return self.collaboration.network_info(topic_uuid)
        return self.session.get_network_info()

    def _node(self, node_uuid: str | None,
              node_type: str) -> ProtocolNode | None:
        node = self.session.protocol.index.get(node_uuid) if node_uuid else None
        return (
            node
            if (
                node
                and node.data.get("type") == node_type
                and self.owns_node(node_uuid)
            )
            else None
        )

    def owns_node(self, node_uuid: str, peer_addr: str | None = None) -> bool:
        """Whether one side's node belongs to an Agreement topic and schema."""
        if peer_addr is not None:
            node = self.session.get_cached_peer_subtree(peer_addr, node_uuid)
            if not node or node.data.get("type") not in self.OWNED_NODE_TYPES:
                return False
            topic_uuids = set(
                self.session.peer_topics_for_node(peer_addr, node_uuid),
            )
            if local_topic := self._local_agreement_topic(node_uuid):
                topic_uuids.add(local_topic.uuid)
            return any(
                (topic := self.session.get_cached_peer_subtree(peer_addr, topic_uuid))
                and topic.data.get("type") == "agreement"
                and self._subtree_contains(topic, node_uuid)
                for topic_uuid in topic_uuids
            )

        node = self.session.protocol.index.get(node_uuid)
        if not node or node.data.get("type") not in self.OWNED_NODE_TYPES:
            return False
        return self._local_agreement_topic(node_uuid) is not None

    def _local_agreement_topic(self, node_uuid: str) -> ProtocolNode | None:
        node = self.session.protocol.index.get(node_uuid)
        if not node:
            return None
        seen = set()
        current = node
        while current and current.uuid not in seen:
            seen.add(current.uuid)
            if current.data.get("type") == "agreement":
                parent = self.session.protocol.index.get(current.parent_uuid)
                return current if (
                    parent
                    and parent.data.get("type") == "agreement_app"
                    and parent.data.get("name") == AGREEMENT_APP_NAME
                ) else None
            current = self.session.protocol.index.get(current.parent_uuid)
        return None

    @staticmethod
    def _subtree_contains(root: ProtocolNode, node_uuid: str) -> bool:
        return root.uuid == node_uuid or any(
            AgreementLogic._subtree_contains(child, node_uuid)
            for child in root.children
        )

    def create_agenda_item(
        self, agreement_uuid: str, text: str, priority: str | None = None,
    ) -> SessionResult:
        agreement = self._node(agreement_uuid, "agreement")
        if not agreement:
            return SessionResult("error", reason="agreement not found")
        return self.session.create_agenda_item(
            agreement.uuid, text, priority,
        )

    def delete_agenda_item(self, item_uuid: str) -> SessionResult:
        if not self.owns_node(item_uuid):
            return SessionResult("error", reason="agenda item not found")
        return self.session.delete_agenda_item(item_uuid)

    def set_agenda_item_priority(
        self, item_uuid: str, priority: str | None,
    ) -> SessionResult:
        if not self.owns_node(item_uuid):
            return SessionResult("error", reason="agenda item not found")
        return self.session.set_agenda_item_priority(item_uuid, priority)

    def move_agenda_item(self, item_uuid: str, index: int) -> SessionResult:
        if not self.owns_node(item_uuid):
            return SessionResult("error", reason="agenda item not found")
        return self.session.move_agenda_item(item_uuid, index)

    def _metadata(self) -> dict:
        """Return a detached read copy of this application's metadata.

        Session hands out the live namespace only to a caller holding its
        lock, so readers snapshot it and writers open their own transaction.
        """
        with self.session.lock:
            return copy.deepcopy(
                self.session.application_metadata(AGREEMENT_APPLICATION_ID),
            )

    def _remember_agreement(self, agreement_uuid: str) -> None:
        with self.session.lock:
            metadata = self.session.application_metadata(
                AGREEMENT_APPLICATION_ID,
            )
            metadata["selected_agreement_uuid"] = agreement_uuid

    def _agreement_container(self) -> ProtocolNode:
        return self._folder(
            self._apps_folder(), AGREEMENT_APP_NAME, "agreement_app",
        )

    def _find_agreement_container(self) -> ProtocolNode | None:
        apps = next(
            (
                child for child in self.session.protocol.root.live_children()
                if child.data.get("type") == "folder"
                and child.data.get("name") == "apps"
            ),
            None,
        )
        if not apps:
            return None
        return next(
            (
                child for child in apps.live_children()
                if child.data.get("type") == "agreement_app"
                and child.data.get("name") == AGREEMENT_APP_NAME
            ),
            None,
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
