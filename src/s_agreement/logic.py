"""Agreement documents and their consent-based organizational hierarchy.

Every agreement remains an independently shared topic.  A parent agreement
contains an ``agreement_link`` that names a child topic, while the child topic
also names its intended parent.  Consequently, accepting a parent never grants
access to its children and a hierarchy is visible only after the relationship
has been accepted in the parent as well.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone

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

    def subagreement_links(self, agreement: ProtocolNode) -> list[ProtocolNode]:
        return self._ordered(agreement, "agreement_link")

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
            decision = self._record_decision(
                result.value, "accepted", None,
            )
            self._remember_agreement(result.value.uuid)
            return SessionResult(
                "ok",
                value=result.value.uuid,
                effects=[*result.effects, *decision.effects],
            )
        return result

    def create_subagreement(
        self, parent_agreement_uuid: str, title: str,
    ) -> SessionResult:
        parent = self._node(parent_agreement_uuid, "agreement")
        if not parent:
            return SessionResult("error", reason="parent agreement not found")
        prerequisites = self._check_parent_chain(parent.uuid)
        if prerequisites.status != "ok":
            return prerequisites
        normalized = str(title or "").strip()
        if not normalized:
            return SessionResult("error", reason="agreement title is required")

        created = self.session.create_child(
            self._agreement_container().uuid,
            {
                "type": "agreement",
                "title": normalized,
                "parent_agreement_uuid": parent.uuid,
            },
            {},
        )
        if created.status != "ok":
            return created
        child = created.value
        linked = self.session.create_child(
            parent.uuid,
            {
                "type": "agreement_link",
                "child_agreement_uuid": child.uuid,
                "title": normalized,
                "order": self.session.next_child_order(
                    parent.uuid, "agreement_link",
                ),
            },
            {},
        )
        if linked.status != "ok":
            self.session.delete(child.uuid)
            return linked
        # Creating the relationship is also an explicit acceptance of the
        # parent's new version. The child itself is a separately accepted
        # topic, recorded by its own decision item.
        parent_decision = self._record_decision(
            self._node(parent.uuid, "agreement"), "accepted", None,
        )
        child_decision = self._record_decision(child, "accepted", None)
        self._remember_agreement(child.uuid)
        return SessionResult(
            "ok",
            value=child.uuid,
            effects=[
                *created.effects,
                *linked.effects,
                *parent_decision.effects,
                *child_decision.effects,
            ],
        )

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
        allowed = self._interaction_guard(agreement)
        if allowed.status != "ok":
            return allowed
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
        allowed = self._interaction_guard_for_node(section.uuid)
        if allowed.status != "ok":
            return allowed
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
        allowed = self._interaction_guard_for_node(clause.uuid)
        if allowed.status != "ok":
            return allowed
        normalized = str(text or "").strip()
        if not normalized:
            return SessionResult("error", reason="clause text is required")
        data = dict(clause.data)
        data["text"] = normalized
        return self.session.modify(clause.uuid, data, clause.weights)

    def rename_agreement(self, agreement_uuid: str, title: str) -> SessionResult:
        result = self._retitle(
            agreement_uuid, "agreement", "title", title,
        )
        if result.status != "ok":
            return result
        normalized = str(title or "").strip()
        effects = list(result.effects)
        for link in self._links_to(agreement_uuid):
            data = dict(link.data)
            data["title"] = normalized
            updated = self.session.modify(link.uuid, data, link.weights)
            if updated.status == "ok":
                effects.extend(updated.effects)
        result.effects = effects
        return result

    def rename_section(self, section_uuid: str, title: str) -> SessionResult:
        return self._retitle(section_uuid, "agreement_section", "title", title)

    def _retitle(self, node_uuid: str, node_type: str, field: str,
                 value: str) -> SessionResult:
        node = self._node(node_uuid, node_type)
        if not node:
            return SessionResult("error", reason=f"{node_type} not found")
        allowed = self._interaction_guard_for_node(node.uuid)
        if allowed.status != "ok":
            return allowed
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
        allowed = self._interaction_guard(agreement)
        if allowed.status != "ok":
            return allowed
        effects = []
        # The child topics are independent agreements, so deleting a parent
        # promotes them rather than cascading through the organization.
        for link in self.subagreement_links(agreement):
            child = self._node(
                link.data.get("child_agreement_uuid"), "agreement",
            )
            if not child:
                continue
            data = dict(child.data)
            data.pop("parent_agreement_uuid", None)
            detached = self.session.modify(child.uuid, data, child.weights)
            if detached.status == "ok":
                effects.extend(detached.effects)
        # Removing a child also proposes removal of its organizational link
        # to every participant in the parent agreement.
        for link in self._links_to(agreement.uuid):
            removed = self.session.delete(link.uuid)
            if removed.status == "ok":
                effects.extend(removed.effects)
        release = self.session.end_topic_sharing(agreement.uuid)
        result = self.session.delete(agreement.uuid)
        if result.status != "ok":
            return result
        result.effects = [*effects, *release.effects, *result.effects]
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
        allowed = self._interaction_guard_for_node(section.uuid)
        if allowed.status != "ok":
            return allowed
        return self.session.delete(section.uuid)

    def delete_clause(self, clause_uuid: str) -> SessionResult:
        clause = self._node(clause_uuid, "agreement_clause")
        if not clause:
            return SessionResult("error", reason="clause not found")
        allowed = self._interaction_guard_for_node(clause.uuid)
        if allowed.status != "ok":
            return allowed
        return self.session.delete(clause.uuid)

    def move_section(self, section_uuid: str, index: int) -> SessionResult:
        if not self._node(section_uuid, "agreement_section"):
            return SessionResult("error", reason="section not found")
        allowed = self._interaction_guard_for_node(section_uuid)
        if allowed.status != "ok":
            return allowed
        return self.session.move_child_to_index(section_uuid, index)

    def move_clause(self, clause_uuid: str, index: int) -> SessionResult:
        if not self._node(clause_uuid, "agreement_clause"):
            return SessionResult("error", reason="clause not found")
        allowed = self._interaction_guard_for_node(clause_uuid)
        if allowed.status != "ok":
            return allowed
        return self.session.move_child_to_index(clause_uuid, index)

    def accept_agreement_invitation(self, subtree: ProtocolNode) -> SessionResult:
        if subtree.data.get("type") != "agreement":
            return SessionResult("error", reason="invited topic is not an agreement")
        parent_uuid = str(
            subtree.data.get("parent_agreement_uuid") or "",
        ).strip()
        if parent_uuid:
            prerequisites = self._check_parent_chain(
                parent_uuid, subtree.uuid,
            )
            if prerequisites.status != "ok":
                return prerequisites
        result = self.session.accept_topic_invitation(
            subtree, self._agreement_container().uuid,
        )
        if result.status == "ok":
            agreement = self._node(result.value, "agreement")
            decision = self._record_decision(
                agreement, "accepted", None,
            )
            result.effects.extend(decision.effects)
            self._remember_agreement(result.value)
        return result

    # Reacting per node is what lets a divergence be left behind. Without it
    # an agreement can reach a state it cannot exit: two sides edit the same
    # clause, both see "diverged", and nothing either of them does resolves
    # it. Both primitives are Session's; this application only names which
    # node types may be reacted to.
    REACTABLE = frozenset({
        "agreement", "agreement_section", "agreement_clause", "agreement_link",
    })
    OWNED_NODE_TYPES = frozenset({
        *REACTABLE, "agenda_item", "agreement_decision",
    })

    def accept_peer_node(self, source_addr: str, node_uuid: str,
                         adopt_absence: bool = False) -> SessionResult:
        local_exists = node_uuid in self.session.protocol.index
        if ((local_exists and not self.owns_node(node_uuid))
                or (not adopt_absence
                    and not self.owns_node(node_uuid, source_addr))):
            return SessionResult("error", reason="node is not part of an agreement")
        allowed = self._interaction_guard_for_reaction(
            source_addr, node_uuid,
        )
        if allowed.status != "ok":
            return allowed
        return self.session.accept_peer_node(source_addr, node_uuid, adopt_absence)

    def rollback_peer_node(self, source_addr: str, node_uuid: str,
                           rollback_absence: bool = False) -> SessionResult:
        if (not self.owns_node(node_uuid)
                or (not rollback_absence
                    and not self.owns_node(node_uuid, source_addr))):
            return SessionResult("error", reason="node is not part of an agreement")
        allowed = self._interaction_guard_for_reaction(
            source_addr, node_uuid,
        )
        if allowed.status != "ok":
            return allowed
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
        allowed = self._interaction_guard_for_node(agreement_uuid)
        if allowed.status != "ok":
            return allowed
        changed = self.session.reconcile_peer_changes(
            source_addr,
            agreement_uuid,
            lambda node, _event_type: (
                node.data.get("type") != "agreement_decision"
            ),
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
                if (
                    not self._is_decision_event(address, event)
                    and not (
                        event["type"] == "in_transition"
                        and liveness.get("state") == "stale"
                    )
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
            "agreement": (
                self._document_node_dict(selected) if selected else None
            ),
            "agreements": [
                self._document_node_dict(node) for node in agreements
            ],
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
            "organization": self.organization_payload(),
            "acceptances": (
                self.acceptance_badges(selected.uuid) if selected else []
            ),
            "interaction": (
                self.interaction_payload(selected) if selected else {
                    "allowed": False, "reason": "",
                }
            ),
            "refusal_consequences": (
                [
                    {
                        "uuid": item.uuid,
                        "title": item.data.get("title")
                        or "Untitled agreement",
                    }
                    for item in self.descendant_agreements(selected.uuid)
                ]
                if selected else []
            ),
        }

    @staticmethod
    def _document_node_dict(node: ProtocolNode) -> dict:
        """Serialize document content without internal decision records."""
        payload = node.to_dict()

        def remove_decisions(item: dict) -> None:
            children = [
                child for child in item.get("children") or []
                if child.get("data", {}).get("type") != "agreement_decision"
            ]
            item["children"] = children
            for child in children:
                remove_decisions(child)

        remove_decisions(payload)
        return payload

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

    def set_decision(
        self,
        agreement_uuid: str,
        decision: str,
        expires_at: str | None = None,
    ) -> SessionResult:
        agreement = self._node(agreement_uuid, "agreement")
        if not agreement:
            return SessionResult("error", reason="agreement not found")
        allowed = self._interaction_guard(agreement)
        if allowed.status != "ok":
            return allowed
        normalized_decision = str(decision or "").strip().lower()
        if normalized_decision not in {"accepted", "refused"}:
            return SessionResult(
                "error", reason="decision must be accepted or refused",
            )
        normalized_expiry = self._normalize_expiry(expires_at)
        if expires_at and normalized_expiry is None:
            return SessionResult(
                "error", reason="expiration must be an ISO date or timestamp",
            )
        result = self._record_decision(
            agreement, normalized_decision, normalized_expiry,
        )
        if result.status == "ok" and normalized_decision == "refused":
            effects = list(result.effects)
            for descendant in self.descendant_agreements(agreement.uuid):
                refused = self._record_decision(
                    descendant, "refused", normalized_expiry,
                )
                if refused.status == "ok":
                    effects.extend(refused.effects)
            result.effects = effects
        if result.status == "ok" and normalized_decision == "accepted":
            # A child invitation may already be cached but deliberately
            # unmounted because an ancestor was not yet accepted.
            self.session.mount_cached_topics(AGREEMENT_APPLICATION_ID)
        return result

    def interaction_payload(self, agreement: ProtocolNode) -> dict:
        result = self._interaction_guard(agreement)
        return {
            "allowed": result.status == "ok",
            "reason": result.reason or "",
        }

    def descendant_agreements(
        self, agreement_uuid: str,
    ) -> list[ProtocolNode]:
        """Locally joined descendants in deterministic parent-first order."""
        root = self._node(agreement_uuid, "agreement")
        if not root:
            return []
        descendants = []
        pending = [root]
        seen = {root.uuid}
        while pending:
            parent = pending.pop(0)
            for link in self.subagreement_links(parent):
                child_uuid = str(
                    link.data.get("child_agreement_uuid") or "",
                ).strip()
                child = self._node(child_uuid, "agreement")
                if (
                    not child
                    or child.uuid in seen
                    or child.data.get("parent_agreement_uuid") != parent.uuid
                ):
                    continue
                seen.add(child.uuid)
                descendants.append(child)
                pending.append(child)
        return descendants

    def acceptance_badges(self, agreement_uuid: str) -> list[dict]:
        agreement = self._node(agreement_uuid, "agreement")
        if not agreement:
            return []
        members = self._topic_members(agreement_uuid)
        identity_by_address = {
            address: member
            for member in members
            for address in member.get("addresses") or [member.get("address")]
            if address
        }
        latest: dict[str, dict] = {}

        def consider(node: ProtocolNode, expected_identity_uuid: str) -> None:
            data = node.data
            if (
                data.get("type") != "agreement_decision"
                or data.get("identity_uuid") != expected_identity_uuid
            ):
                return
            current = latest.get(expected_identity_uuid)
            if (
                current is None
                or str(data.get("decided_at") or "")
                > str(current.get("decided_at") or "")
            ):
                latest[expected_identity_uuid] = dict(data)

        for child in agreement.live_children():
            consider(child, self.session.identity.uuid)
        for address in self.session.peer_addresses(agreement_uuid):
            member = identity_by_address.get(address)
            if not member:
                continue
            peer_topic = self.session.get_cached_peer_subtree(
                address, agreement_uuid,
            )
            if not peer_topic:
                continue
            for child in peer_topic.live_children():
                consider(child, member["uuid"])

        current_reference = self.agreement_reference_hash(agreement)
        badges = []
        for member in members:
            record = latest.get(member["uuid"])
            status = "pending"
            if record:
                if record.get("decision") == "refused":
                    status = "refused"
                elif self._is_expired(record.get("expires_at")):
                    status = "expired"
                elif record.get("reference_hash") != current_reference:
                    status = "outdated"
                elif record.get("decision") == "accepted":
                    status = "accepted"
            badges.append({
                **member,
                "status": status,
                "decision": record.get("decision") if record else None,
                "decided_at": record.get("decided_at") if record else None,
                "reference_hash": (
                    record.get("reference_hash") if record else None
                ),
                "expires_at": record.get("expires_at") if record else None,
                "current_reference_hash": current_reference,
            })
        return badges

    def agreement_reference_hash(self, agreement: ProtocolNode) -> str:
        """Hash agreement content without participant decision records."""
        included_types = {
            "agreement", "agreement_section", "agreement_clause",
            "agreement_link",
        }

        def content(node: ProtocolNode) -> dict | None:
            if node.deleted or node.data.get("type") not in included_types:
                return None
            children = [
                item
                for child in node.children
                if (item := content(child)) is not None
            ]
            children.sort(key=lambda item: item["uuid"])
            return {
                "uuid": node.uuid,
                "data": copy.deepcopy(node.data),
                "weights": copy.deepcopy(node.weights),
                "children": children,
            }

        encoded = json.dumps(
            content(agreement),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def _record_decision(
        self,
        agreement: ProtocolNode | None,
        decision: str,
        expires_at: str | None,
    ) -> SessionResult:
        if not agreement:
            return SessionResult("error", reason="agreement not found")
        data = {
            "type": "agreement_decision",
            "identity_uuid": self.session.identity.uuid,
            "decision": decision,
            "decided_at": self._now(),
            "reference_hash": self.agreement_reference_hash(agreement),
            "expires_at": expires_at,
        }
        existing = max(
            (
                child for child in agreement.live_children()
                if (
                    child.data.get("type") == "agreement_decision"
                    and child.data.get("identity_uuid")
                    == self.session.identity.uuid
                )
            ),
            key=lambda node: str(node.data.get("decided_at") or ""),
            default=None,
        )
        if existing:
            return self.session.modify(existing.uuid, data, existing.weights)
        return self.session.create_child(agreement.uuid, data, {})

    def _check_parent_chain(
        self,
        parent_uuid: str,
        expected_child_uuid: str | None = None,
    ) -> SessionResult:
        seen = set()
        while parent_uuid:
            if parent_uuid in seen:
                return SessionResult(
                    "error", reason="agreement hierarchy contains a cycle",
                )
            seen.add(parent_uuid)
            parent = self._node(parent_uuid, "agreement")
            if not parent:
                return SessionResult(
                    "error",
                    reason=(
                        "Join and accept every parent agreement before "
                        "joining this subagreement"
                    ),
                )
            if expected_child_uuid and not any(
                link.data.get("child_agreement_uuid") == expected_child_uuid
                for link in self.subagreement_links(parent)
            ):
                return SessionResult(
                    "error",
                    reason=(
                        "The parent agreement has not accepted this "
                        "subagreement yet"
                    ),
                )
            if not self._has_current_acceptance(parent):
                title = parent.data.get("title") or "parent agreement"
                return SessionResult(
                    "error",
                    reason=(
                        f"Accept the current version of {title} before "
                        "joining its subagreement"
                    ),
                )
            expected_child_uuid = parent.uuid
            parent_uuid = str(
                parent.data.get("parent_agreement_uuid") or "",
            ).strip()
        return SessionResult("ok")

    def _has_current_acceptance(self, agreement: ProtocolNode) -> bool:
        own = max(
            (
                child for child in agreement.live_children()
                if (
                    child.data.get("type") == "agreement_decision"
                    and child.data.get("identity_uuid")
                    == self.session.identity.uuid
                )
            ),
            key=lambda node: str(node.data.get("decided_at") or ""),
            default=None,
        )
        return bool(
            own
            and own.data.get("decision") == "accepted"
            and not self._is_expired(own.data.get("expires_at"))
            and own.data.get("reference_hash")
            == self.agreement_reference_hash(agreement)
        )

    def _topic_members(self, agreement_uuid: str) -> list[dict]:
        identities = self.session.known_identities()
        by_address = {
            address: identity
            for identity in identities
            for address in (
                identity.get("addresses") or [identity.get("address")]
            )
            if address
        }
        self_identity = next(
            (
                item for item in identities
                if item.get("uuid") == self.session.identity.uuid
            ),
            {},
        )
        people = [{
            "uuid": self.session.identity.uuid,
            "name": self_identity.get("name") or "You",
            "picture": self_identity.get("picture") or "",
            "address": self.session.address,
            "addresses": [self.session.address],
            "is_self": True,
        }]
        seen = {self.session.identity.uuid}
        for address in self.session.peer_addresses(agreement_uuid):
            identity = by_address.get(address) or {}
            identity_uuid = identity.get("uuid") or f"address:{address}"
            if identity_uuid in seen:
                continue
            seen.add(identity_uuid)
            people.append({
                "uuid": identity_uuid,
                "name": identity.get("name") or address,
                "picture": identity.get("picture") or "",
                "address": address,
                "addresses": identity.get("addresses") or [address],
                "is_self": False,
            })
        return people

    def _is_decision_event(self, peer_addr: str, event: dict) -> bool:
        node_uuid = event.get("node_uuid")
        local = self.session.protocol.index.get(node_uuid) if node_uuid else None
        peer = (
            self.session.get_cached_peer_subtree(peer_addr, node_uuid)
            if node_uuid else None
        )
        return any(
            node and node.data.get("type") == "agreement_decision"
            for node in (local, peer)
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _normalize_expiry(value: str | None) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z",
        )

    @staticmethod
    def _is_expired(value: str | None) -> bool:
        normalized = AgreementLogic._normalize_expiry(value)
        if not normalized:
            return False
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        return parsed <= datetime.now(timezone.utc)

    def organization_payload(self) -> dict:
        """Return the locally consented agreement hierarchy.

        Membership is topic-scoped: a person appears on an agreement only
        when this Session knows that peer to discuss that exact topic.
        """
        agreements = self.agreements()
        summaries = {
            agreement.uuid: {
                "uuid": agreement.uuid,
                "title": agreement.data.get("title") or "Untitled agreement",
                "joined": True,
                "interaction_allowed": (
                    self._interaction_guard(agreement).status == "ok"
                ),
                "declared_parent_uuid": agreement.data.get(
                    "parent_agreement_uuid",
                ),
                "members": self._topic_members(agreement.uuid),
                "children": [],
            }
            for agreement in agreements
        }
        parent_for: dict[str, str] = {}
        children_for: dict[str, list[dict]] = {
            agreement.uuid: [] for agreement in agreements
        }

        def creates_cycle(parent_uuid: str, child_uuid: str) -> bool:
            cursor = parent_uuid
            seen = set()
            while cursor and cursor not in seen:
                if cursor == child_uuid:
                    return True
                seen.add(cursor)
                cursor = parent_for.get(cursor, "")
            return False

        for parent in agreements:
            for link in self.subagreement_links(parent):
                child_uuid = str(
                    link.data.get("child_agreement_uuid") or "",
                ).strip()
                if not child_uuid or child_uuid == parent.uuid:
                    continue
                child = summaries.get(child_uuid)
                if child is not None:
                    # Both topics must agree about the same parent.  A link
                    # alone cannot reorganize somebody else's agreement.
                    if (
                        child.get("declared_parent_uuid") != parent.uuid
                        or child_uuid in parent_for
                        or creates_cycle(parent.uuid, child_uuid)
                    ):
                        continue
                    parent_for[child_uuid] = parent.uuid
                    children_for[parent.uuid].append({
                        "uuid": child_uuid,
                        "joined": True,
                    })
                else:
                    children_for[parent.uuid].append({
                        "uuid": child_uuid,
                        "title": (
                            link.data.get("title")
                            or "Restricted subagreement"
                        ),
                        "joined": False,
                        "interaction_allowed": False,
                        "declared_parent_uuid": parent.uuid,
                        "members": [],
                        "children": [],
                    })

        def build(uuid: str) -> dict:
            summary = dict(summaries[uuid])
            declared = summary.get("declared_parent_uuid")
            if declared and uuid not in parent_for:
                summary["relationship_status"] = "awaiting_parent_agreement"
            else:
                summary["relationship_status"] = (
                    "linked" if uuid in parent_for else "root"
                )
            summary["children"] = [
                (
                    build(child["uuid"])
                    if child.get("joined")
                    else dict(child, relationship_status="linked")
                )
                for child in children_for[uuid]
            ]
            return summary

        roots = [
            build(agreement.uuid)
            for agreement in agreements
            if agreement.uuid not in parent_for
        ]
        return {"roots": roots, "agreement_count": len(agreements)}

    def _interaction_guard(self, agreement: ProtocolNode) -> SessionResult:
        parent_uuid = str(
            agreement.data.get("parent_agreement_uuid") or "",
        ).strip()
        expected_child_uuid = agreement.uuid
        seen = set()
        while parent_uuid:
            if parent_uuid in seen:
                return SessionResult(
                    "error", reason="Read-only: agreement hierarchy has a cycle",
                )
            seen.add(parent_uuid)
            parent = self._node(parent_uuid, "agreement")
            if not parent:
                return SessionResult(
                    "error",
                    reason="Read-only until every parent agreement is joined",
                )
            if not any(
                link.data.get("child_agreement_uuid") == expected_child_uuid
                for link in self.subagreement_links(parent)
            ):
                return SessionResult(
                    "error",
                    reason=(
                        "Read-only until the parent accepts this "
                        "subagreement relationship"
                    ),
                )
            if not self._has_current_acceptance(parent):
                title = parent.data.get("title") or "Parent agreement"
                return SessionResult(
                    "error",
                    reason=(
                        f"Read-only because {title} is not currently accepted"
                    ),
                )
            expected_child_uuid = parent.uuid
            parent_uuid = str(
                parent.data.get("parent_agreement_uuid") or "",
            ).strip()
        return SessionResult("ok")

    def _interaction_guard_for_node(self, node_uuid: str) -> SessionResult:
        agreement = self._local_agreement_topic(node_uuid)
        if not agreement:
            return SessionResult("error", reason="agreement not found")
        return self._interaction_guard(agreement)

    def _interaction_guard_for_reaction(
        self, peer_addr: str, node_uuid: str,
    ) -> SessionResult:
        if local := self._local_agreement_topic(node_uuid):
            return self._interaction_guard(local)
        for topic_uuid in self.session.peer_topics_for_node(
            peer_addr, node_uuid,
        ):
            agreement = self._node(topic_uuid, "agreement")
            if agreement:
                return self._interaction_guard(agreement)
        return SessionResult("error", reason="agreement not found")

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

    def _links_to(self, agreement_uuid: str) -> list[ProtocolNode]:
        return [
            link
            for agreement in self.agreements()
            for link in self.subagreement_links(agreement)
            if link.data.get("child_agreement_uuid") == agreement_uuid
        ]

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
        allowed = self._interaction_guard(agreement)
        if allowed.status != "ok":
            return allowed
        return self.session.create_agenda_item(
            agreement.uuid, text, priority,
        )

    def delete_agenda_item(self, item_uuid: str) -> SessionResult:
        if not self.owns_node(item_uuid):
            return SessionResult("error", reason="agenda item not found")
        allowed = self._interaction_guard_for_node(item_uuid)
        if allowed.status != "ok":
            return allowed
        return self.session.delete_agenda_item(item_uuid)

    def set_agenda_item_priority(
        self, item_uuid: str, priority: str | None,
    ) -> SessionResult:
        if not self.owns_node(item_uuid):
            return SessionResult("error", reason="agenda item not found")
        allowed = self._interaction_guard_for_node(item_uuid)
        if allowed.status != "ok":
            return allowed
        return self.session.set_agenda_item_priority(item_uuid, priority)

    def move_agenda_item(self, item_uuid: str, index: int) -> SessionResult:
        if not self.owns_node(item_uuid):
            return SessionResult("error", reason="agenda item not found")
        allowed = self._interaction_guard_for_node(item_uuid)
        if allowed.status != "ok":
            return allowed
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
