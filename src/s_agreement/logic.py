"""Agreement documents and their consent-based organizational hierarchy.

Every agreement remains an independently shared topic.  A subagreement is an
Agreement holding a role in its parent: the parent carries an ordinary role
offered to an Agreement actor, and the child carries an
``agreement_role_holding`` naming which role in which parent.  Consequently,
accepting a parent never grants access to its children, and a hierarchy is
visible only once the seat has been taken on both sides.
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
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
        # Reading Session.identity snapshots the whole protocol tree, and
        # building one payload asked for it hundreds of times - once per
        # role, per holder, per member list - which cost over a second per
        # build. Reads memoise inside a scope; nothing outside one does, so
        # a mutation can never see a stale entry.
        self._memo: dict | None = None
        self.session.identity
        with self.session.lock:
            self.session.application_metadata(AGREEMENT_APPLICATION_ID)

    @contextmanager
    def _reading(self):
        """Memoise repeated lookups for the length of one read.

        Only reads open a scope, and a read performs no mutation, so nothing
        cached here can go stale while it is in use. Nested scopes share the
        outermost one, since a payload builds the organization inside itself.
        """
        outer = self._memo
        if outer is None:
            self._memo = {}
        try:
            yield
        finally:
            if outer is None:
                self._memo = None

    def _cached(self, key, build):
        if self._memo is None:
            return build()
        if key not in self._memo:
            self._memo[key] = build()
        return self._memo[key]

    @property
    def _identity_uuid(self) -> str:
        return self._cached(("identity",), lambda: self.session.identity.uuid)

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

    # A subagreement is not a link any more: it is an Agreement holding a
    # role in its parent, the same shape as a person holding one. The parent
    # side is an ordinary role with an offer to an Agreement actor; the child
    # side is an agreement_role_holding naming which role in which parent.
    #
    # The child keeps its own record rather than only the parent holding one,
    # because somebody who has joined the child but not the parent still has
    # to know a parent exists - that is what tells them the agreement is
    # read-only until they join it.

    def parent_holdings(self, agreement: ProtocolNode) -> list[ProtocolNode]:
        """This agreement's roles in other agreements, in declared order."""
        return self._ordered(agreement, "agreement_role_holding")

    def child_agreements(
        self, agreement: ProtocolNode,
    ) -> list[tuple[str, ProtocolNode]]:
        """(child agreement uuid, the role it was offered) for each subunit."""
        found = []
        for role in self.roles(agreement):
            for offer in self.role_offers(role):
                if offer.data.get("actor_kind") == "agreement":
                    child_uuid = str(offer.data.get("actor_uuid") or "").strip()
                    if child_uuid:
                        found.append((child_uuid, role))
        return found

    def _agreement_holds_role(
        self, role: ProtocolNode, actor_uuid: str,
    ) -> bool:
        """Whether an Agreement actor's holding of this role is live.

        Offer plus accepted answer, exactly as for a person. Deliberately not
        checking that the answer is against the current version: that would
        mean every edit to a parent freezes every subagreement until somebody
        re-accepts on each one's behalf, on top of each person re-accepting
        their own roles. Left for step 3b to decide with the ANY-path guard.
        """
        if not self._offer_for(role, actor_uuid):
            return False
        answer = next(
            (
                child for child in role.live_children()
                if (
                    child.data.get("type") == "agreement_role_decision"
                    and child.data.get("actor_uuid") == actor_uuid
                )
            ),
            None,
        )
        return bool(
            answer
            and answer.data.get("decision") == "accepted"
            and not self._is_expired(answer.data.get("expires_at"))
        )

    def _holding_is_live(
        self, holder: ProtocolNode, holding: ProtocolNode,
    ) -> bool:
        """Whether `holder` really occupies the seat this holding names.

        The holder is passed rather than looked up, because this also runs
        against an invited subtree that is not in the local index yet - which
        is the whole point of checking it before mounting.
        """
        parent = self._node(
            str(holding.data.get("parent_agreement_uuid") or "").strip(),
            "agreement",
        )
        role = self._node(
            str(holding.data.get("role_uuid") or "").strip(), "agreement_role",
        )
        # The role has to be one of that parent's own: a holding naming
        # somebody else's role says nothing about this relationship.
        owner = self._local_agreement_topic(role.uuid) if role else None
        if not parent or not role or not owner or owner.uuid != parent.uuid:
            return False
        return self._agreement_holds_role(role, holder.uuid)

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
            identity = self._create_identity(result.value)
            participant = self._create_default_participant(result.value)
            self._remember_agreement(result.value.uuid)
            return SessionResult(
                "ok",
                value=result.value.uuid,
                effects=[
                    *result.effects, *identity.effects,
                    *participant.effects,
                ],
            )
        return result

    def create_subagreement(
        self, parent_agreement_uuid: str, title: str,
    ) -> SessionResult:
        """Make a seat in the parent and fill it with a new agreement."""
        parent = self._node(parent_agreement_uuid, "agreement")
        if not parent:
            return SessionResult("error", reason="parent agreement not found")
        normalized = str(title or "").strip()
        if not normalized:
            return SessionResult("error", reason="agreement title is required")
        role = self.create_role(parent.uuid, normalized)
        if role.status != "ok":
            return role
        seated = self.create_seated_agreement(role.value, normalized)
        if seated.status != "ok":
            self.session.delete(role.value)
            return seated
        seated.effects = [*role.effects, *seated.effects]
        return seated

    def create_seated_agreement(
        self, role_uuid: str, title: str,
    ) -> SessionResult:
        """Fill a role with a new agreement.

        This is how structure is made: a seat exists, and an agreement is
        created to take it. The creator holds the new agreement's Identity,
        which is what lets them answer for it straight away.
        """
        role = self._node(role_uuid, "agreement_role")
        parent = self._local_agreement_topic(role.uuid) if role else None
        if not role or not parent:
            return SessionResult("error", reason="role not found")
        prerequisites = self._check_parent_chain(parent)
        if prerequisites.status != "ok":
            return prerequisites
        if not self.holds_identity(parent):
            return SessionResult(
                "error", reason="only the Identity holder can offer a role",
            )
        normalized = str(title or "").strip()
        if not normalized:
            return SessionResult("error", reason="agreement title is required")

        created = self.session.create_child(
            self._agreement_container().uuid,
            {"type": "agreement", "title": normalized},
            {},
        )
        if created.status != "ok":
            return created
        child = created.value
        identity = self._create_identity(child)
        participant = self._create_default_participant(child)
        offered = self.offer_role(role.uuid, child.uuid)
        if offered.status != "ok":
            self.session.delete(child.uuid)
            return offered
        seated = self.seat_agreement(role.uuid, child.uuid)
        if seated.status != "ok":
            self.session.delete(child.uuid)
            return seated
        self._remember_agreement(child.uuid)
        return SessionResult(
            "ok",
            value=child.uuid,
            effects=[
                *created.effects, *identity.effects, *participant.effects,
                *offered.effects, *seated.effects,
            ],
        )

    # What a copy carries. Everything an agreement holds beyond this is
    # somebody's record of taking part in it - Identity, offers, answers, the
    # seats it holds elsewhere - and copying the text is not copying who
    # agreed to it.
    CLONED_TYPES = frozenset({
        "agreement_section", "agreement_clause",
        "agreement_role", "agreement_accountability", "agreement_domain",
    })

    def clone_agreement(
        self, agreement_uuid: str, title: str | None = None,
    ) -> SessionResult:
        """Copy an agreement's text and structure, with nobody in it.

        Fresh uuids throughout, because acceptance is uuid-keyed: a copy that
        shared role uuids with its original would let an answer given there
        count here. Nothing is stripped afterwards - the records of taking
        part are simply never copied - so the copy lands at zero actors,
        which is what a template is (2.8). No Identity and no default
        Participant either: taking Identity is how somebody starts using it.

        Not gated on holding a role in the source. Copying reads that
        agreement and writes only a new one of this session's own, so an
        agreement you can see read-only is one you can fork into a template.
        """
        source = self._node(agreement_uuid, "agreement")
        if not source:
            return SessionResult("error", reason="agreement not found")
        normalized = str(title or "").strip() or (
            f"{source.data.get('title') or 'Untitled agreement'} (template)"
        )
        created = self.session.create_child(
            self._agreement_container().uuid,
            {"type": "agreement", "title": normalized},
            {},
        )
        if created.status != "ok":
            return created
        copied = self._copy_content(source, created.value.uuid)
        if copied.status != "ok":
            self.session.delete(created.value.uuid)
            return copied
        self._remember_agreement(created.value.uuid)
        return SessionResult(
            "ok",
            value=created.value.uuid,
            effects=[*created.effects, *copied.effects],
        )

    def _copy_content(
        self, source: ProtocolNode, target_uuid: str,
    ) -> SessionResult:
        """Recreate a node's copyable children under a new parent."""
        effects = []
        for child in source.live_children():
            if child.data.get("type") not in self.CLONED_TYPES:
                continue
            # Order rides along in the data, so the children can be walked in
            # any order and still come out arranged as they were.
            created = self.session.create_child(
                target_uuid, dict(child.data), dict(child.weights),
            )
            if created.status != "ok":
                return created
            copied = self._copy_content(child, created.value.uuid)
            if copied.status != "ok":
                return copied
            effects.extend([*created.effects, *copied.effects])
        return SessionResult("ok", effects=effects)

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
        # The seat in the parent keeps its own name. It is what the parent
        # expects of this body, which is not the same thing as what the
        # body calls itself.
        return self._retitle(agreement_uuid, "agreement", "title", title)

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
        # promotes them rather than cascading through the organization:
        # their side of the seat goes, and they become roots.
        for child_uuid, _role in self.child_agreements(agreement):
            child = self._node(child_uuid, "agreement")
            if not child:
                continue
            for holding in self.parent_holdings(child):
                if holding.data.get("parent_agreement_uuid") != agreement.uuid:
                    continue
                dropped = self.session.delete(holding.uuid)
                if dropped.status == "ok":
                    effects.extend(dropped.effects)
        # And the answers given on this agreement's behalf elsewhere go with
        # it, so no parent is left showing a seat filled by something that is
        # gone from here. Its own side of each seat needs no separate delete:
        # the holdings are inside the subtree about to go.
        for holding in self.parent_holdings(agreement):
            effects.extend(self._release_seat(agreement, holding))
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

    # Identity is the one role whose holding is a single node rather than an
    # offer plus a self-reported decision. It can be, because it is singular:
    # everybody writes the holder into the same node, so the protocol's own
    # semantics carry the consent. Both replicas naming the same holder is
    # agreement; naming different holders is a divergence. Handover and a
    # contested claim are therefore the same event, settled with the same
    # adopt/rollback buttons every other node already has.
    #
    # It stays out of agreement_reference_hash deliberately: a handover
    # changes who holds a role, not what the agreement says, and must not
    # re-open everybody's acceptance.

    def identity_holder(self, agreement: ProtocolNode) -> str:
        """The actor this replica currently sees holding Identity."""
        nodes = self._identity_nodes(agreement)
        if len(nodes) != 1:
            return ""
        return str(nodes[0].data.get("holder_actor_uuid") or "").strip()

    def holds_identity(self, agreement: ProtocolNode) -> bool:
        return bool(
            (holder := self.identity_holder(agreement))
            and holder == self._identity_uuid
        )

    def _holds_identity_of(self, agreement_uuid: str) -> bool:
        """Same question about an agreement named only by uuid, which may be
        one this session has not joined and so cannot answer for."""
        agreement = self._node(agreement_uuid, "agreement")
        return bool(agreement and self.holds_identity(agreement))

    def take_identity(self, agreement_uuid: str) -> SessionResult:
        """Install this participant as Identity.

        Deliberately not gated on the seat being free. Somebody has to be
        able to act when a holder is gone, and no rule evaluated against an
        observer-relative view can tell "vacant" from "I do not sync with the
        holder". Taking an occupied seat writes a competing holder into the
        same node, which surfaces as a divergence for both sides to settle.
        The warning belongs in the interface, not in a refusal here.
        """
        return self._write_identity(agreement_uuid, self._identity_uuid)

    def offer_identity(
        self, agreement_uuid: str, actor_uuid: str,
    ) -> SessionResult:
        """Hand Identity on. It is theirs once they adopt the node."""
        agreement = self._node(agreement_uuid, "agreement")
        if not agreement:
            return SessionResult("error", reason="agreement not found")
        if not self.holds_identity(agreement):
            return SessionResult(
                "error", reason="only the Identity holder can hand it over",
            )
        normalized = str(actor_uuid or "").strip()
        if not normalized:
            return SessionResult("error", reason="an actor is required")
        if normalized == self._identity_uuid:
            return SessionResult("error", reason="Identity is already yours")
        return self._write_identity(agreement_uuid, normalized)

    def _write_identity(
        self, agreement_uuid: str, actor_uuid: str,
    ) -> SessionResult:
        agreement = self._node(agreement_uuid, "agreement")
        if not agreement:
            return SessionResult("error", reason="agreement not found")
        allowed = self._interaction_guard(agreement)
        if allowed.status != "ok":
            return allowed
        nodes = self._identity_nodes(agreement)
        # Two records is the one case the single-node encoding cannot settle
        # by itself, because two distinct nodes never diverge against each
        # other. It is reachable only for an agreement that predates Identity
        # and was then claimed on two sides at once, so it is surfaced rather
        # than guessed at.
        if len(nodes) > 1:
            return SessionResult(
                "error",
                reason=(
                    "This agreement has more than one Identity record. "
                    "Settle that before changing it."
                ),
            )
        if not nodes:
            return self._create_identity(agreement, actor_uuid)
        node = nodes[0]
        data = dict(node.data)
        data["holder_actor_uuid"] = actor_uuid
        data["held_since"] = self._now()
        return self.session.modify(node.uuid, data, node.weights)

    def _create_default_participant(
        self, agreement: ProtocolNode,
    ) -> SessionResult:
        """Every agreement starts with one role, taken by its creator.

        Without it a new agreement has nobody in it, and taking part would
        mean first inventing the role to take. The name and purpose are
        ordinary editable content - this is a starting point, not a fixture.
        """
        created = self.session.create_child(
            agreement.uuid,
            {
                "type": "agreement_role",
                "name": "Participant",
                "purpose": "Take part in this agreement",
                "order": 0.0,
            },
            {},
        )
        if created.status != "ok":
            return created
        role = created.value
        offered = self.session.create_child(
            role.uuid,
            {
                "type": "agreement_role_offer",
                "actor_uuid": self._identity_uuid,
                "actor_kind": "individual",
                "offered_by": self._identity_uuid,
                "offered_at": self._now(),
            },
            {},
        )
        decided = self._record_role_decision(
            agreement, role, "accepted", None,
        )
        return SessionResult(
            "ok",
            value=role.uuid,
            effects=[*created.effects, *offered.effects, *decided.effects],
        )

    def _create_identity(
        self, agreement: ProtocolNode, actor_uuid: str | None = None,
    ) -> SessionResult:
        return self.session.create_child(
            agreement.uuid,
            {
                "type": "agreement_identity",
                "holder_actor_uuid": actor_uuid or self._identity_uuid,
                "held_since": self._now(),
            },
            {},
        )

    @staticmethod
    def _identity_nodes(agreement: ProtocolNode) -> list[ProtocolNode]:
        return [
            child for child in agreement.live_children()
            if child.data.get("type") == "agreement_identity"
        ]

    def identity_payload(self, agreement: ProtocolNode) -> dict:
        """Who holds Identity here, and what any peer says instead."""
        blank = {
            "node_uuid": "",
            "holder_actor_uuid": "",
            "holder_name": "",
            "is_self": False,
            "held_since": None,
            "claims": [],
        }
        nodes = self._identity_nodes(agreement)
        if len(nodes) > 1:
            return {**blank, "state": "ambiguous"}
        if not nodes:
            return {**blank, "state": "vacant"}

        people = self._topic_members(agreement.uuid)
        members = {member["uuid"]: member for member in people}
        uuid_for_address = {
            address: member["uuid"]
            for member in people
            for address in member.get("addresses") or [member.get("address")]
            if address
        }

        def describe(actor_uuid: str) -> str:
            return (
                (members.get(actor_uuid) or {}).get("name")
                or "Someone you have not met"
            )

        node = nodes[0]
        holder = str(node.data.get("holder_actor_uuid") or "").strip()
        # What each peer's own copy of this node says. The view needs it to
        # tell a handover - where the peer naming a new holder *is* that new
        # holder - from a claim staked over somebody still in the seat.
        claims = []
        for address in self.session.peer_addresses(agreement.uuid):
            peer_topic = self.session.get_cached_peer_subtree(
                address, agreement.uuid,
            )
            if not peer_topic:
                continue
            peer_node = next(
                (
                    child for child in peer_topic.live_children()
                    if child.uuid == node.uuid
                ),
                None,
            )
            if not peer_node:
                continue
            peer_holder = str(
                peer_node.data.get("holder_actor_uuid") or "",
            ).strip()
            if peer_holder and peer_holder != holder:
                claims.append({
                    "peer_addr": address,
                    "holder_actor_uuid": peer_holder,
                    "holder_name": describe(peer_holder),
                    # The same divergence means two different things. A peer
                    # naming *itself* is accepting a handover; a peer naming
                    # somebody else is staking a claim over whoever is still
                    # in the seat.
                    "is_handover": (
                        uuid_for_address.get(address) == peer_holder
                    ),
                })
        return {
            "node_uuid": node.uuid,
            "state": "held",
            "holder_actor_uuid": holder,
            "holder_name": describe(holder),
            "is_self": holder == self._identity_uuid,
            "held_since": node.data.get("held_since"),
            "claims": claims,
        }

    # Roles are document content: a role is part of what people agree to,
    # not administration layered on top of it. Accountabilities and domains
    # are the same shape - text plus order, owned by a role - so they share
    # one set of write paths. They are separate node types rather than a
    # list inside the role's data because REACTABLE is per node: two people
    # editing different accountabilities have to diverge separately, exactly
    # as two clauses do. A list would collapse both edits into one
    # undiffable conflict.
    ROLE_ITEM_TYPES = {
        "accountability": "agreement_accountability",
        "domain": "agreement_domain",
    }

    def roles(self, agreement: ProtocolNode) -> list[ProtocolNode]:
        return self._ordered(agreement, "agreement_role")

    def accountabilities(self, role: ProtocolNode) -> list[ProtocolNode]:
        return self._ordered(role, "agreement_accountability")

    def domains(self, role: ProtocolNode) -> list[ProtocolNode]:
        return self._ordered(role, "agreement_domain")

    def create_role(self, agreement_uuid: str, name: str) -> SessionResult:
        agreement = self._node(agreement_uuid, "agreement")
        if not agreement:
            return SessionResult("error", reason="agreement not found")
        allowed = self._interaction_guard(agreement)
        if allowed.status != "ok":
            return allowed
        normalized = str(name or "").strip()
        if not normalized:
            return SessionResult("error", reason="role name is required")
        result = self.session.create_child(
            agreement.uuid,
            {
                "type": "agreement_role",
                "name": normalized,
                "purpose": "",
                "order": self.session.next_child_order(
                    agreement.uuid, "agreement_role",
                ),
            },
            {},
        )
        if result.status == "ok":
            return SessionResult(
                "ok", value=result.value.uuid, effects=result.effects,
            )
        return result

    def rename_role(self, role_uuid: str, name: str) -> SessionResult:
        return self._retitle(role_uuid, "agreement_role", "name", name)

    def set_role_purpose(self, role_uuid: str, purpose: str) -> SessionResult:
        # A purpose may be cleared. Unlike the name it does not identify the
        # role, so _retitle's "required" rule would be wrong here.
        role = self._node(role_uuid, "agreement_role")
        if not role:
            return SessionResult("error", reason="role not found")
        allowed = self._interaction_guard_for_node(role.uuid)
        if allowed.status != "ok":
            return allowed
        data = dict(role.data)
        data["purpose"] = str(purpose or "").strip()
        return self.session.modify(role.uuid, data, role.weights)

    def delete_role(self, role_uuid: str) -> SessionResult:
        role = self._node(role_uuid, "agreement_role")
        if not role:
            return SessionResult("error", reason="role not found")
        allowed = self._interaction_guard_for_node(role.uuid)
        if allowed.status != "ok":
            return allowed
        return self.session.delete(role.uuid)

    def move_role(self, role_uuid: str, index: int) -> SessionResult:
        if not self._node(role_uuid, "agreement_role"):
            return SessionResult("error", reason="role not found")
        allowed = self._interaction_guard_for_node(role_uuid)
        if allowed.status != "ok":
            return allowed
        return self.session.move_child_to_index(role_uuid, index)

    def create_role_item(
        self, role_uuid: str, kind: str, text: str,
    ) -> SessionResult:
        node_type = self.ROLE_ITEM_TYPES.get(str(kind or "").strip())
        if not node_type:
            return SessionResult(
                "error", reason="kind must be accountability or domain",
            )
        role = self._node(role_uuid, "agreement_role")
        if not role:
            return SessionResult("error", reason="role not found")
        allowed = self._interaction_guard_for_node(role.uuid)
        if allowed.status != "ok":
            return allowed
        normalized = str(text or "").strip()
        if not normalized:
            return SessionResult("error", reason=f"{kind} text is required")
        result = self.session.create_child(
            role.uuid,
            {
                "type": node_type,
                "text": normalized,
                "order": self.session.next_child_order(role.uuid, node_type),
            },
            {},
        )
        if result.status == "ok":
            return SessionResult(
                "ok", value=result.value.uuid, effects=result.effects,
            )
        return result

    def update_role_item(self, item_uuid: str, text: str) -> SessionResult:
        item = self._role_item(item_uuid)
        if not item:
            return SessionResult("error", reason="role item not found")
        allowed = self._interaction_guard_for_node(item.uuid)
        if allowed.status != "ok":
            return allowed
        normalized = str(text or "").strip()
        if not normalized:
            return SessionResult("error", reason="text is required")
        data = dict(item.data)
        data["text"] = normalized
        return self.session.modify(item.uuid, data, item.weights)

    def delete_role_item(self, item_uuid: str) -> SessionResult:
        item = self._role_item(item_uuid)
        if not item:
            return SessionResult("error", reason="role item not found")
        allowed = self._interaction_guard_for_node(item.uuid)
        if allowed.status != "ok":
            return allowed
        return self.session.delete(item.uuid)

    def move_role_item(self, item_uuid: str, index: int) -> SessionResult:
        if not self._role_item(item_uuid):
            return SessionResult("error", reason="role item not found")
        allowed = self._interaction_guard_for_node(item_uuid)
        if allowed.status != "ok":
            return allowed
        return self.session.move_child_to_index(item_uuid, index)

    # Holding a role is two records with two authors. The Identity holder
    # writes the offer; the actor writes their own decision. A holding is
    # live only while both are present, which is what lets either side end
    # it alone: revoking deletes the offer, resigning deletes the decision,
    # and neither party can touch the other's node.
    #
    # They are siblings under the role rather than nested, because deleting
    # a container prunes its descendants - nesting the decision under the
    # offer would make revocation delete the actor's own record.
    #
    # An offer is per (role, actor), not per role: a role may be held by
    # several actors, and revoking one must not revoke the rest.

    def role_offers(self, role: ProtocolNode) -> list[ProtocolNode]:
        """Offers that still stand. A revoked one is kept but is not an offer."""
        return [
            offer for offer in self._all_role_offers(role)
            if not offer.data.get("revoked_at")
        ]

    @staticmethod
    def _all_role_offers(role: ProtocolNode) -> list[ProtocolNode]:
        return [
            child for child in role.live_children()
            if child.data.get("type") == "agreement_role_offer"
        ]

    def offer_role(self, role_uuid: str, actor_uuid: str) -> SessionResult:
        role = self._node(role_uuid, "agreement_role")
        if not role:
            return SessionResult("error", reason="role not found")
        agreement = self._local_agreement_topic(role.uuid)
        allowed = self._interaction_guard(agreement)
        if allowed.status != "ok":
            return allowed
        if not self.holds_identity(agreement):
            return SessionResult(
                "error", reason="only the Identity holder can offer a role",
            )
        normalized = str(actor_uuid or "").strip()
        if not normalized:
            return SessionResult("error", reason="an actor is required")
        if self._offer_for(role, normalized):
            return SessionResult(
                "error", reason="that actor has already been offered this role",
            )
        data = {
            "type": "agreement_role_offer",
            "actor_uuid": normalized,
            # An agreement can be offered a seat as readily as a person.
            "actor_kind": (
                "agreement"
                if self._node(normalized, "agreement") else "individual"
            ),
            "offered_by": self._identity_uuid,
            "offered_at": self._now(),
            "revoked_at": None,
        }
        # Offering again after a revocation revives the same record rather
        # than laying a second one beside it.
        if withdrawn := self._offer_for(role, normalized, revoked=True):
            return self.session.modify(
                withdrawn.uuid, data, withdrawn.weights,
            )
        return self.session.create_child(role.uuid, data, {})

    def revoke_role_offer(
        self, role_uuid: str, actor_uuid: str,
    ) -> SessionResult:
        """Withdraw an offer. The actor's own decision is theirs and stays.

        The offer is marked rather than deleted. Deleting it would leave the
        actor's surviving answer indistinguishable from somebody asking for
        the role unprompted, so the Identity holder would immediately be
        asked to re-offer what they had just withdrawn. Marking is still a
        withdrawal of what Identity itself wrote, so the authorship rule
        holds; it just keeps the fact that there was an offer.
        """
        role = self._node(role_uuid, "agreement_role")
        if not role:
            return SessionResult("error", reason="role not found")
        agreement = self._local_agreement_topic(role.uuid)
        allowed = self._interaction_guard(agreement)
        if allowed.status != "ok":
            return allowed
        if not self.holds_identity(agreement):
            return SessionResult(
                "error", reason="only the Identity holder can revoke a role",
            )
        offer = self._offer_for(role, str(actor_uuid or "").strip())
        if not offer:
            return SessionResult("error", reason="offer not found")
        data = dict(offer.data)
        data["revoked_at"] = self._now()
        data["revoked_by"] = self._identity_uuid
        return self.session.modify(offer.uuid, data, offer.weights)

    def decide_role(
        self, role_uuid: str, decision: str, expires_at: str | None = None,
    ) -> SessionResult:
        """Record this participant's own answer about a role.

        An answer with no matching offer is a *request*: somebody saying they
        will take this role, waiting on the Identity holder to confirm it.
        Nothing else is needed to express that, because a holding is live
        only while both records exist - so an offer alone is an unfilled
        seat, and a decision alone is a request. It is also how a newcomer
        gets their first role at all: they hold nothing, so they cannot be
        offered anything by anyone but Identity, and asking is the move
        available to them.
        """
        role = self._node(role_uuid, "agreement_role")
        if not role:
            return SessionResult("error", reason="role not found")
        agreement = self._local_agreement_topic(role.uuid)
        allowed = self._interaction_guard(agreement)
        if allowed.status != "ok":
            return allowed
        mine = self._identity_uuid
        if not self._offer_for(role, mine):
            # There may be an offer that has only reached this session as a
            # proposal. If there is, answering it should take it up; if there
            # is not, this answer stands on its own as a request.
            self._adopt_offer_proposal(agreement, role, mine)
            role = self._node(role_uuid, "agreement_role") or role
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
        recorded = self._record_role_decision(
            agreement, role, normalized_decision, normalized_expiry,
        )
        if recorded.status == "ok" and normalized_decision == "accepted":
            # A subagreement invitation may already be cached and deliberately
            # unmounted because this session held nothing in an ancestor. It
            # does now.
            self.session.mount_cached_topics(AGREEMENT_APPLICATION_ID)
        return recorded

    def seat_agreement(
        self, role_uuid: str, agreement_uuid: str,
    ) -> SessionResult:
        """Take a role on behalf of an agreement whose Identity is held here.

        The counterpart of decide_role for an Agreement actor. An agreement
        cannot answer for itself, so whoever holds its Identity answers for
        it, and decided_by records who that was.
        """
        return self._answer_seat(role_uuid, agreement_uuid, "accepted")

    def decline_seat(
        self, role_uuid: str, agreement_uuid: str,
    ) -> SessionResult:
        """Turn down a seat offered to an agreement, on its behalf.

        Without this an invitation an agreement does not want sits unanswered
        forever, since the offer belongs to the parent and only its author may
        withdraw it (2.3). The refusal is the agreement's own record, so it is
        written by the only person who can speak for it.
        """
        return self._answer_seat(role_uuid, agreement_uuid, "refused")

    def _answer_seat(
        self, role_uuid: str, agreement_uuid: str, decision: str,
    ) -> SessionResult:
        role = self._node(role_uuid, "agreement_role")
        if not role:
            return SessionResult("error", reason="role not found")
        parent = self._local_agreement_topic(role.uuid)
        seated = self._node(agreement_uuid, "agreement")
        if not parent or not seated:
            return SessionResult("error", reason="agreement not found")
        if not self.holds_identity(seated):
            return SessionResult(
                "error",
                reason="only its Identity holder can answer for that agreement",
            )
        if seated.uuid == parent.uuid:
            return SessionResult(
                "error", reason="an agreement cannot be seated in itself",
            )
        if not self._offer_for(role, seated.uuid):
            # The offer may have reached this session only as a proposal, the
            # same way a person's does. Answering it takes it up first.
            self._adopt_offer_proposal(parent, role, seated.uuid)
            role = self._node(role_uuid, "agreement_role") or role
            parent = self._node(parent.uuid, "agreement") or parent
            if not self._offer_for(role, seated.uuid):
                return SessionResult(
                    "error", reason="this role has not been offered to it",
                )
        if decision == "refused":
            return self._record_role_decision(
                parent, role, "refused", None,
                actor_uuid=seated.uuid, decided_by=self._identity_uuid,
            )
        # Only accepting can close a loop, so the walk is spent only there.
        if self._creates_cycle(seated.uuid, parent.uuid):
            return SessionResult(
                "error",
                reason="that would make the organisation circular",
            )
        answered = self._record_role_decision(
            parent, role, "accepted", None,
            actor_uuid=seated.uuid, decided_by=self._identity_uuid,
        )
        if answered.status != "ok":
            return answered
        seated = self._node(agreement_uuid, "agreement") or seated
        existing = next(
            (
                holding for holding in self.parent_holdings(seated)
                if holding.data.get("role_uuid") == role.uuid
            ),
            None,
        )
        if existing:
            # Reconsidering a refusal: the answer is rewritten and the seat it
            # already names is the same seat.
            return SessionResult("ok", value=existing.uuid, effects=answered.effects)
        held = self.session.create_child(
            seated.uuid,
            {
                "type": "agreement_role_holding",
                "parent_agreement_uuid": parent.uuid,
                "role_uuid": role.uuid,
                "order": self.session.next_child_order(
                    seated.uuid, "agreement_role_holding",
                ),
            },
            {},
        )
        if held.status != "ok":
            return held
        self.session.mount_cached_topics(AGREEMENT_APPLICATION_ID)
        return SessionResult(
            "ok",
            value=held.value.uuid,
            effects=[*answered.effects, *held.effects],
        )

    def seat_offers(self, agreement: ProtocolNode) -> list[dict]:
        """Seats offered to this agreement that it does not yet hold.

        An invitation to an agreement is written on the *parent's* page, so
        without collecting it here the only person who could see it is
        somebody looking at the parent - who need not be the person entitled
        to answer. The answer belongs to whoever holds this agreement's
        Identity, so the invitation is put where that answer is made.
        """
        held = {
            str(holding.data.get("role_uuid") or "").strip()
            for holding in self.parent_holdings(agreement)
        }
        offers = []
        for parent in self.agreements():
            if parent.uuid == agreement.uuid:
                continue
            for role in self.roles(parent):
                if role.uuid in held:
                    continue
                offer = self._offer_for(role, agreement.uuid)
                if offer and offer.data.get("revoked_at"):
                    continue
                if not offer and not self._offer_proposed_to(
                    parent, role, agreement.uuid,
                ):
                    continue
                answer = self._role_decision_for(role, agreement.uuid)
                offers.append({
                    "role_uuid": role.uuid,
                    "role_name": role.data.get("name") or "Untitled role",
                    "role_purpose": role.data.get("purpose") or "",
                    "agreement_uuid": parent.uuid,
                    "title": parent.data.get("title") or "Untitled agreement",
                    "offered_at": offer.data.get("offered_at") if offer else None,
                    # Not adopted here yet. Answering adopts it, so this is a
                    # note about where the record is, not a reason to wait.
                    "proposed": not offer,
                    "answer": (answer.data.get("decision") if answer else ""),
                    # An accepted seat that would close a loop is shown and
                    # refused, rather than hidden as if it had never come.
                    "circular": self._creates_cycle(agreement.uuid, parent.uuid),
                })
        return offers

    def unseat_agreement(
        self, role_uuid: str, agreement_uuid: str,
    ) -> SessionResult:
        """Give up a seat, from the seated agreement's side.

        Both records go, because a holding is live only while both exist:
        dropping this side alone would leave the parent still showing the seat
        as accepted while the agreement no longer claims it, and neither view
        is wrong on its own - they simply contradict each other.
        """
        seated = self._node(agreement_uuid, "agreement")
        if not seated:
            return SessionResult("error", reason="agreement not found")
        if not self.holds_identity(seated):
            return SessionResult(
                "error",
                reason="only its Identity holder can give up that seat",
            )
        held = [
            holding for holding in self.parent_holdings(seated)
            if holding.data.get("role_uuid") == role_uuid
        ]
        # Whether the seat was held is read from the holdings themselves. An
        # unshared agreement produces no effects at all, so counting those
        # would call every offline release a failure.
        if not held:
            return SessionResult("error", reason="it does not hold that role")
        effects = []
        for holding in held:
            effects.extend(self._release_seat(seated, holding))
            dropped = self.session.delete(holding.uuid)
            if dropped.status == "ok":
                effects.extend(dropped.effects)
        return SessionResult("ok", effects=effects)

    def _release_seat(
        self, seated: ProtocolNode, holding: ProtocolNode,
    ) -> list:
        """Withdraw the answer written in the parent for one seat.

        The Agreement actor's resign_role: the only record this side wrote up
        there is the acceptance, so that is all that goes. The role and the
        offer are the parent's, and a role may seat several actors at once -
        deleting it to empty one seat would take everybody else's with it.
        Leaving the offer standing makes the seat unfilled rather than never
        offered, which is what lets it be answered again.
        """
        role = self._node(
            str(holding.data.get("role_uuid") or "").strip(), "agreement_role",
        )
        decision = self._role_decision_for(role, seated.uuid) if role else None
        if not decision:
            return []
        removed = self.session.delete(decision.uuid)
        return list(removed.effects) if removed.status == "ok" else []

    def move_parent_holding(
        self, holding_uuid: str, index: int,
    ) -> SessionResult:
        """Reorder which parent is preferred as home."""
        holding = self._node(holding_uuid, "agreement_role_holding")
        if not holding:
            return SessionResult("error", reason="holding not found")
        agreement = self._local_agreement_topic(holding.uuid)
        if not agreement or not self.holds_identity(agreement):
            return SessionResult(
                "error",
                reason="only its Identity holder can reorder its parents",
            )
        return self.session.move_child_to_index(holding.uuid, index)

    def parent_payload(self, agreement: ProtocolNode) -> list[dict]:
        """Every seat this agreement holds, in order, home first."""
        home = self.home_parent_uuid(agreement)
        out = []
        for holding in self.parent_holdings(agreement):
            parent_uuid = str(
                holding.data.get("parent_agreement_uuid") or "",
            ).strip()
            parent = self._node(parent_uuid, "agreement")
            role = self._node(
                str(holding.data.get("role_uuid") or "").strip(),
                "agreement_role",
            )
            out.append({
                "holding_uuid": holding.uuid,
                "agreement_uuid": parent_uuid,
                "title": (
                    parent.data.get("title") if parent
                    else "An agreement you have not joined"
                ),
                "role_uuid": role.uuid if role else "",
                "role_name": role.data.get("name") if role else "",
                "joined": bool(parent),
                "is_home": parent_uuid == home and bool(home),
                "live": self._holding_problem(
                    agreement, holding, {agreement.uuid},
                ) is None,
            })
        return out

    def offerable_actors(self, agreement: ProtocolNode) -> list[dict]:
        """Everyone and everything that could be offered a role here."""
        actors = [
            {
                "uuid": person.get("uuid"),
                "name": person.get("name") or person.get("address") or "",
                "kind": "individual",
            }
            for person in self.session.known_identities()
        ]
        for other in self.agreements():
            if other.uuid == agreement.uuid:
                continue
            if self._creates_cycle(other.uuid, agreement.uuid):
                continue
            actors.append({
                "uuid": other.uuid,
                "name": other.data.get("title") or "Untitled agreement",
                "kind": "agreement",
            })
        return actors

    def resign_role(self, role_uuid: str) -> SessionResult:
        """Step out of a role. Deleting only what this participant wrote."""
        role = self._node(role_uuid, "agreement_role")
        if not role:
            return SessionResult("error", reason="role not found")
        allowed = self._interaction_guard_for_node(role.uuid)
        if allowed.status != "ok":
            return allowed
        decision = self._own_role_decision(role)
        if not decision:
            return SessionResult("error", reason="you do not hold this role")
        return self.session.delete(decision.uuid)

    def _record_role_decision(
        self,
        agreement: ProtocolNode,
        role: ProtocolNode,
        decision: str,
        expires_at: str | None,
        actor_uuid: str | None = None,
        decided_by: str | None = None,
    ) -> SessionResult:
        """One answer per actor per role, rewritten rather than added to.

        An actor who refuses and later accepts has changed their mind, not
        answered twice, and two decision nodes for one actor would leave
        which of them counts up to iteration order. `decided_by` is set only
        for an Agreement actor, which cannot answer for itself.
        """
        actor = actor_uuid or self._identity_uuid
        data = {
            "type": "agreement_role_decision",
            "actor_uuid": actor,
            "decision": decision,
            "decided_at": self._now(),
            "reference_hash": self.role_reference_hash(agreement, role),
            "expires_at": expires_at,
        }
        if decided_by:
            data["decided_by"] = decided_by
        existing = self._role_decision_for(role, actor)
        if existing:
            return self.session.modify(existing.uuid, data, existing.weights)
        return self.session.create_child(role.uuid, data, {})

    def _adopt_offer_proposal(
        self, agreement: ProtocolNode, role: ProtocolNode, actor_uuid: str,
    ) -> SessionResult:
        """Take up an offer that has only reached this session as a proposal.

        This application never merges a peer's new node automatically - it
        presents it for adoption - and an offer is no exception. Answering
        one that has not been adopted yet therefore has to adopt it first.
        Doing that here keeps it a single gesture for the person: they were
        offered a role, and they answer. The authority check is not skipped,
        because this goes through the same accept_peer_node as any other
        adoption.
        """
        for address in self.session.peer_addresses(agreement.uuid):
            peer_topic = self.session.get_cached_peer_subtree(
                address, agreement.uuid,
            )
            peer_role = (
                self._find_in_subtree(peer_topic, role.uuid)
                if peer_topic else None
            )
            if not peer_role:
                continue
            found = next(
                (
                    child for child in peer_role.live_children()
                    if (
                        child.data.get("type") == "agreement_role_offer"
                        and child.data.get("actor_uuid") == actor_uuid
                    )
                ),
                None,
            )
            if found:
                return self.accept_peer_node(address, found.uuid)
        return SessionResult(
            "error", reason="this role has not been offered to you",
        )

    def _offer_for(
        self, role: ProtocolNode, actor_uuid: str, revoked: bool = False,
    ) -> ProtocolNode | None:
        source = (
            self._all_role_offers(role) if revoked else self.role_offers(role)
        )
        return next(
            (
                offer for offer in source
                if offer.data.get("actor_uuid") == actor_uuid
            ),
            None,
        )

    def _own_role_decision(self, role: ProtocolNode) -> ProtocolNode | None:
        return self._role_decision_for(role, self._identity_uuid)

    def _role_decision_for(
        self, role: ProtocolNode, actor_uuid: str,
    ) -> ProtocolNode | None:
        return next(
            (
                child for child in role.live_children()
                if (
                    child.data.get("type") == "agreement_role_decision"
                    and child.data.get("actor_uuid") == actor_uuid
                )
            ),
            None,
        )

    def role_holders(
        self, agreement: ProtocolNode, role: ProtocolNode,
    ) -> list[dict]:
        """Everyone offered this role, and where their answer stands.

        A decision is credible only from the actor's own replica, so an
        answer this session cannot reach is reported as unobserved rather
        than guessed at or shown as pending. Those are different facts: one
        is "they have not answered", the other is "I cannot see whether they
        have".
        """
        return self._cached(
            ("holders", agreement.uuid, role.uuid),
            lambda: self._build_role_holders(agreement, role),
        )

    def _build_role_holders(
        self, agreement: ProtocolNode, role: ProtocolNode,
    ) -> list[dict]:
        people = {
            member["uuid"]: member
            for member in self._topic_members(agreement.uuid)
        }
        current = self.role_reference_hash(agreement, role)
        offers = {
            str(offer.data.get("actor_uuid") or "").strip(): offer
            for offer in self._all_role_offers(role)
        }
        decisions = self._observed_decisions(agreement, role)
        holders = []
        for actor_uuid in offers.keys() | decisions.keys():
            offer = offers.get(actor_uuid)
            record = decisions.get(actor_uuid)
            member = people.get(actor_uuid)
            # Somebody this session knows but who is not on this topic is
            # a different case from somebody it cannot place at all: the
            # first has simply not been invited here, which is actionable
            # and is not the same as being unable to see their answer.
            known = None if member else self._known_people().get(actor_uuid)
            revoked = bool(offer and offer.data.get("revoked_at"))
            if revoked and not record:
                # Withdrawn, and nobody left holding an answer to it. The
                # record stays so the offer can be revived, but there is no
                # longer anyone involved to show.
                continue
            # An Agreement actor is never among the people on this topic, so
            # the member test below would call every one of them a stranger.
            # Its standing is read from the answer given on its behalf.
            is_agreement = bool(
                offer and offer.data.get("actor_kind") == "agreement"
            )
            if revoked:
                status = "revoked"
            elif not offer:
                # An answer nobody offered: somebody asking to take this.
                status = "requested"
            elif not member and not is_agreement:
                status = "uninvited" if known else "unobserved"
            elif not record:
                # For an agreement, no answer means either that nobody has
                # given one or that whoever could is out of reach - and only
                # its Identity holder can tell those apart.
                status = (
                    "unobserved" if is_agreement and not self._holds_identity_of(
                        actor_uuid,
                    ) else "pending"
                )
            elif record.get("decision") == "refused":
                status = "refused"
            elif self._is_expired(record.get("expires_at")):
                status = "expired"
            elif record.get("reference_hash") != current:
                status = "outdated"
            else:
                status = "accepted"
            # An Agreement actor is not among the people on this topic, so it
            # is named by the agreement it is, when that is joined here.
            seated = (
                self._node(actor_uuid, "agreement")
                if (offer and offer.data.get("actor_kind") == "agreement")
                else None
            )
            holders.append({
                "actor_uuid": actor_uuid,
                # A request that has since been confirmed: the offer exists,
                # but only as a proposal on the peer that wrote it. Nothing
                # merges here without somebody's act, so the person who asked
                # still has to take it up - they are told, rather than left
                # watching a request that looks unanswered forever.
                "confirmed_elsewhere": (
                    status == "requested"
                    and actor_uuid == self._identity_uuid
                    and self._offer_proposed_to(agreement, role, actor_uuid)
                ),
                "name": (
                    (seated.data.get("title") or "Untitled agreement")
                    if seated
                    # A profile with no display name still has an
                    # address, which names somebody better than a
                    # placeholder saying they are a stranger.
                    else (member or known or {}).get("name")
                    or (member or known or {}).get("address")
                    or "Somebody you have not met"
                ),
                "joined": bool(seated) if (
                    offer and offer.data.get("actor_kind") == "agreement"
                ) else None,
                # Individual or Agreement. The view draws them differently,
                # because "a person holds this" and "a body holds this" are
                # not the same fact.
                "actor_kind": (
                    (offer.data.get("actor_kind") if offer else None)
                    or "individual"
                ),
                "picture": (member or {}).get("picture") or "",
                "is_self": actor_uuid == self._identity_uuid,
                "status": status,
                "decided_at": (record or {}).get("decided_at"),
                "expires_at": (record or {}).get("expires_at"),
                "offered_at": (offer.data.get("offered_at") if offer else None),
                "offered_by": (offer.data.get("offered_by") if offer else None),
            })
        return sorted(holders, key=lambda item: (item["status"], item["name"]))

    def _offer_proposed_to(
        self, agreement: ProtocolNode, role: ProtocolNode, actor_uuid: str,
    ) -> bool:
        """Whether some peer holds an offer of this role to that actor."""
        mine = actor_uuid
        for address in self.session.peer_addresses(agreement.uuid):
            peer_topic = self.session.get_cached_peer_subtree(
                address, agreement.uuid,
            )
            peer_role = (
                self._find_in_subtree(peer_topic, role.uuid)
                if peer_topic else None
            )
            if peer_role and any(
                child.data.get("type") == "agreement_role_offer"
                and child.data.get("actor_uuid") == mine
                and not child.data.get("revoked_at")
                for child in peer_role.live_children()
            ):
                return True
        return False

    def _observed_decisions(
        self, agreement: ProtocolNode, role: ProtocolNode,
    ) -> dict[str, dict]:
        """Every answer about this role this session can actually vouch for.

        Only from the replica of the person it belongs to: a peer's copy of a
        third party's answer is hearsay, and nothing signs content, so it is
        not counted. That is also what makes an unreachable answer reportable
        as unobserved rather than invented.

        An Agreement actor has no replica of its own - it cannot answer for
        itself - so the replica that vouches for it is the one belonging to
        whoever answered on its behalf. Same rule, applied to who gave the
        answer rather than to whose answer it is.
        """
        found: dict[str, dict] = {}
        own = self._own_role_decision(role)
        if own:
            found[self._identity_uuid] = dict(own.data)
        found.update(self._answers_given_by(role, self._identity_uuid))
        members = {
            address: member
            for member in self._topic_members(agreement.uuid)
            for address in member.get("addresses") or [member.get("address")]
            if address
        }
        for address in self.session.peer_addresses(agreement.uuid):
            member = members.get(address)
            if not member:
                continue
            peer_topic = self.session.get_cached_peer_subtree(
                address, agreement.uuid,
            )
            peer_role = (
                self._find_in_subtree(peer_topic, role.uuid)
                if peer_topic else None
            )
            if not peer_role:
                continue
            for child in peer_role.live_children():
                if (
                    child.data.get("type") == "agreement_role_decision"
                    and child.data.get("actor_uuid") == member["uuid"]
                ):
                    found[member["uuid"]] = dict(child.data)
            found.update(self._answers_given_by(peer_role, member["uuid"]))
        return found

    @staticmethod
    def _answers_given_by(role: ProtocolNode, actor_uuid: str) -> dict[str, dict]:
        """Answers this actor recorded on some agreement's behalf.

        `decided_by` is set only where an actor could not answer for itself,
        so its presence is what marks an answer as given rather than owned.
        """
        return {
            str(child.data.get("actor_uuid") or ""): dict(child.data)
            for child in role.live_children()
            if (
                child.data.get("type") == "agreement_role_decision"
                and child.data.get("decided_by") == actor_uuid
            )
        }

    @staticmethod
    def _find_in_subtree(
        root: ProtocolNode, node_uuid: str,
    ) -> ProtocolNode | None:
        if root.uuid == node_uuid:
            return root
        for child in root.children:
            if found := AgreementLogic._find_in_subtree(child, node_uuid):
                return found
        return None

    def _role_item(self, item_uuid: str) -> ProtocolNode | None:
        # A stored node names its own kind, so only creation has to be told
        # which one it is.
        for node_type in self.ROLE_ITEM_TYPES.values():
            if node := self._node(item_uuid, node_type):
                return node
        return None

    def accept_agreement_invitation(self, subtree: ProtocolNode) -> SessionResult:
        if subtree.data.get("type") != "agreement":
            return SessionResult("error", reason="invited topic is not an agreement")
        # The invited subtree carries its own holdings, so its ancestry can
        # be checked before it is mounted. Joining it does not require
        # holding anything in it - that comes after, by asking.
        prerequisites = self._joining_guard(subtree)
        if prerequisites.status != "ok":
            return prerequisites
        result = self.session.accept_topic_invitation(
            subtree, self._agreement_container().uuid,
        )
        if result.status == "ok":
            # Joining is not holding. The newcomer arrives able to read
            # and asks for a role from there (2.4b).
            self._remember_agreement(result.value)
        return result

    # Reacting per node is what lets a divergence be left behind. Without it
    # an agreement can reach a state it cannot exit: two sides edit the same
    # clause, both see "diverged", and nothing either of them does resolves
    # it. Both primitives are Session's; this application only names which
    # node types may be reacted to.
    REACTABLE = frozenset({
        "agreement", "agreement_section", "agreement_clause",
        "agreement_role", "agreement_accountability", "agreement_domain",
        "agreement_role_holding",
        "agreement_identity", "agreement_role_offer", "agreement_role_decision",
    })
    OWNED_NODE_TYPES = frozenset({
        *REACTABLE, "agenda_item",
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
        authority = self._offer_authority_guard(source_addr, node_uuid)
        if authority.status != "ok":
            return authority
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
                node.data.get("type") != "agreement_role_decision"
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
        with self._reading():
            return self._build_document_payload(agreement_uuid, network)

    def _build_document_payload(
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
            "identity_uuid": self._identity_uuid,
            "known_identities": self.session.known_identities(),
            "organization": self.organization_payload(),
            "participants": (
                self.participants(selected.uuid) if selected else []
            ),
            # Whether this session holds a role here. Taking a role is the
            # only way to be part of an agreement, so this is what the view
            # asks before offering anything that only a participant does.
            "holds_role": (
                self._has_current_acceptance(selected) if selected else False
            ),
            "identity": (
                self.identity_payload(selected) if selected
                else {"state": "vacant"}
            ),
            # Resolved here rather than in the view: a holder's status
            # depends on peer replicas the browser never sees.
            "role_holders": (
                {
                    role.uuid: self.role_holders(selected, role)
                    for role in self.roles(selected)
                } if selected else {}
            ),
            "holds_identity": (
                self.holds_identity(selected) if selected else False
            ),
            # Template, instantiated or working - a count of actors, not a
            # kind of node (2.8).
            "state": self.agreement_state(selected) if selected else "",
            "offerable_actors": (
                self.offerable_actors(selected) if selected else []
            ),
            # Where this agreement is drawn, and every other seat it
            # holds - never hidden, or deleting the first parent would
            # take away something load-bearing nobody could see.
            "parents": self.parent_payload(selected) if selected else [],
            # Seats offered to this agreement. They are written on the
            # parent's page, so they are brought here, where the only person
            # who can answer them is looking.
            "seat_offers": self.seat_offers(selected) if selected else [],
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

    # Records about the agreement rather than content of it. They have their
    # own storage nodes and their own presentation - badges, an Identity line
    # - so they stay out of the document serialization and are never rendered
    # as document-change proposals. Their divergences are unaffected:
    # transition events come from the protocol tree, not from this view.
    NON_DOCUMENT_TYPES = frozenset({
        "agreement_identity",
        "agreement_role_offer", "agreement_role_decision",
    })

    @classmethod
    def _document_node_dict(cls, node: ProtocolNode) -> dict:
        """Serialize document content without the records kept beside it."""
        payload = node.to_dict()

        def remove_records(item: dict) -> None:
            children = [
                child for child in item.get("children") or []
                if child.get("data", {}).get("type")
                not in cls.NON_DOCUMENT_TYPES
            ]
            item["children"] = children
            for child in children:
                remove_records(child)

        remove_records(payload)
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
            "identity_uuid": self._identity_uuid,
            "known_identities": self.session.known_identities(),
        }

    def _network_info(self, topic_uuid: str | None) -> dict:
        if self.collaboration:
            return self.collaboration.network_info(topic_uuid)
        return self.session.get_network_info()

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
            for child_uuid, role in self.child_agreements(parent):
                child = self._node(child_uuid, "agreement")
                if not child or child.uuid in seen:
                    continue
                # Both sides have to name the same seat. A role offered to
                # an agreement that never took it is not a subagreement.
                if not any(
                    holding.data.get("parent_agreement_uuid") == parent.uuid
                    and holding.data.get("role_uuid") == role.uuid
                    for holding in self.parent_holdings(child)
                ):
                    continue
                seen.add(child.uuid)
                descendants.append(child)
                pending.append(child)
        return descendants

    def participants(self, agreement_uuid: str) -> list[dict]:
        with self._reading():
            return self._build_participants(agreement_uuid)

    def _build_participants(self, agreement_uuid: str) -> list[dict]:
        """Everyone on this topic, and what each of them holds.

        Two populations that used to be one. *Peers* are who this session
        syncs the topic with; *actors* are who has taken a role. Neither
        contains the other: somebody present holding nothing is an observer,
        visible but not part of the agreement, and somebody holding a role
        this session cannot reach is the reverse, listed with their answer
        unobserved rather than guessed at.
        """
        agreement = self._node(agreement_uuid, "agreement")
        if not agreement:
            return []
        people = {
            member["uuid"]: {**member, "actor_kind": "individual", "roles": []}
            for member in self._topic_members(agreement_uuid)
        }
        for role in self.roles(agreement):
            for holder in self.role_holders(agreement, role):
                person = people.get(holder["actor_uuid"])
                if person is None:
                    person = people[holder["actor_uuid"]] = {
                        "uuid": holder["actor_uuid"],
                        "name": holder["name"],
                        "picture": holder["picture"],
                        "actor_kind": holder["actor_kind"],
                        "address": "",
                        "addresses": [],
                        "is_self": holder["is_self"],
                        "roles": [],
                    }
                person["roles"].append({
                    "uuid": role.uuid,
                    "name": role.data.get("name") or "Untitled role",
                    "status": holder["status"],
                    "decided_at": holder["decided_at"],
                    "expires_at": holder["expires_at"],
                })
        for person in people.values():
            person["is_observer"] = not any(
                item["status"] == "accepted" for item in person["roles"]
            )
        return sorted(
            people.values(),
            key=lambda item: (
                item["is_observer"], not item["is_self"], item["name"],
            ),
        )

    # How many actors are in it is the whole of what distinguishes a template
    # from a working agreement (2.8). No flag, no separate node type, no mode
    # to switch: a template becomes real by being joined and goes back to
    # being one by being left.
    def actor_uuids(self, agreement: ProtocolNode) -> set[str]:
        """Every actor currently holding something here.

        Identity counts, because holding Identity is holding a role. A
        request does not: an answer with no offer behind it is somebody
        asking to be in, which is not the same as being in.
        """
        actors = set()
        if holder := self.identity_holder(agreement):
            actors.add(holder)
        for role in self.roles(agreement):
            actors.update(
                holder["actor_uuid"]
                for holder in self.role_holders(agreement, role)
                if holder["status"] == "accepted"
            )
        return actors

    def agreement_state(self, agreement: ProtocolNode) -> str:
        count = len(self.actor_uuids(agreement))
        if not count:
            return "template"
        return "instantiated" if count == 1 else "working"

    def role_reference_hash(
        self, agreement: ProtocolNode, role: ProtocolNode,
    ) -> str:
        """What accepting this role commits you to.

        The document body plus this role's own definition, and nothing else.
        Hashing the whole agreement would mean editing the Treasurer's
        accountabilities re-opens the Secretary's acceptance and every
        subagreement's; scoping it this way keeps the churn proportional to
        what actually changed for that person.
        """
        return self._cached(
            ("role_hash", agreement.uuid, role.uuid),
            lambda: self._build_role_reference_hash(agreement, role),
        )

    def _build_role_reference_hash(
        self, agreement: ProtocolNode, role: ProtocolNode,
    ) -> str:
        body = self._cached(
            ("body_hash", agreement.uuid),
            lambda: self.agreement_reference_hash(agreement),
        )
        definition = self._content_hash(role, {
            "agreement_role", "agreement_accountability", "agreement_domain",
        })
        combined = f"{body}|{definition}".encode("utf-8")
        return f"sha256:{hashlib.sha256(combined).hexdigest()}"

    def agreement_reference_hash(self, agreement: ProtocolNode) -> str:
        """Hash agreement content without participant decision records.

        Role nodes are deliberately absent. An acceptance covers the document
        body plus the definitions of the roles that participant *holds*, so
        that editing one role does not re-open everybody else's acceptance.
        Nobody holds a role yet, which makes the held-role contribution empty
        and this hash exactly right for now; the scoping becomes visible when
        holdings arrive.
        """
        return self._content_hash(agreement, {
            "agreement", "agreement_section", "agreement_clause",
        })

    @staticmethod
    def _content_hash(root: ProtocolNode, included_types: set[str]) -> str:
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
            content(root),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def _ancestry_problem(
        self, agreement: ProtocolNode,
    ) -> tuple[str, str] | None:
        """The first thing standing between this agreement and a root.

        An agreement is reachable when *any* of its holdings leads to a root
        with every step of that path live. Holding a role in two agreements
        means either can carry it, so one parent going invalid suspends that
        relationship rather than paralysing a body the other still depends
        on. Returns None when reachable, otherwise the problem on the first
        holding in order - the one that would be home if it worked.

        Roots have no holdings and are always reachable.
        """
        return self._path_problem(agreement, {agreement.uuid})

    def _path_problem(
        self, agreement: ProtocolNode, visiting: set[str],
    ) -> tuple[str, str] | None:
        holdings = self.parent_holdings(agreement)
        if not holdings:
            return None
        first: tuple[str, str] | None = None
        for holding in holdings:
            problem = self._holding_problem(agreement, holding, visiting)
            if problem is None:
                return None
            first = first or problem
        return first

    def _holding_problem(
        self, holder: ProtocolNode, holding: ProtocolNode, visiting: set[str],
    ) -> tuple[str, str] | None:
        parent_uuid = str(
            holding.data.get("parent_agreement_uuid") or "",
        ).strip()
        if parent_uuid in visiting:
            return ("cycle", "")
        parent = self._node(parent_uuid, "agreement")
        if not parent:
            return ("unjoined", "")
        if not self._holding_is_live(holder, holding):
            return ("unseated", parent.data.get("title") or "")
        if not self._has_current_acceptance(parent):
            return ("stale", parent.data.get("title") or "Parent agreement")
        return self._path_problem(parent, visiting | {parent_uuid})

    def home_parent_uuid(self, agreement: ProtocolNode) -> str:
        """Where this agreement is drawn: the first holding that works.

        Derived, never stored, so it reverses itself when a parent recovers
        and there is no home field to keep in step. Display and navigation
        only - it must never enter the guard, or "reachable by any path"
        quietly becomes "reachable through home".
        """
        for holding in self.parent_holdings(agreement):
            if self._holding_problem(
                agreement, holding, {agreement.uuid},
            ) is None:
                return str(
                    holding.data.get("parent_agreement_uuid") or "",
                ).strip()
        return ""

    def _creates_cycle(self, child_uuid: str, parent_uuid: str) -> bool:
        """Whether seating `child` under `parent` would close a loop.

        Best-effort per replica: only agreements this session has joined can
        be walked, so a cycle may exist globally that nobody sees whole.
        """
        pending = [parent_uuid]
        seen: set[str] = set()
        while pending:
            uuid = pending.pop()
            if uuid == child_uuid:
                return True
            if uuid in seen:
                continue
            seen.add(uuid)
            node = self._node(uuid, "agreement")
            if not node:
                continue
            pending.extend(
                str(holding.data.get("parent_agreement_uuid") or "").strip()
                for holding in self.parent_holdings(node)
            )
        return False

    def _check_parent_chain(self, parent: ProtocolNode) -> SessionResult:
        """Whether a subagreement may be seated under `parent`.

        Unlike the read-only guard this includes `parent` itself: hanging
        something below an agreement means taking part in that agreement, not
        merely being able to reach it. The guard proper excludes the
        agreement being written to, which is why a root is always writable.
        """
        if not self._has_current_acceptance(parent):
            title = parent.data.get("title") or "parent agreement"
            return SessionResult(
                "error",
                reason=(
                    f"Accept the current version of {title} before joining "
                    "its subagreement"
                ),
            )
        return self._joining_guard(parent)

    def _joining_guard(self, agreement: ProtocolNode) -> SessionResult:
        problem = self._ancestry_problem(agreement)
        if not problem:
            return SessionResult("ok")
        kind, title = problem
        return SessionResult("error", reason={
            "cycle": "agreement hierarchy contains a cycle",
            "unjoined": (
                "Join and accept every parent agreement before joining this "
                "subagreement"
            ),
            "unseated": (
                "The parent agreement has not accepted this subagreement yet"
            ),
            "stale": (
                f"Accept the current version of {title} before joining its "
                "subagreement"
            ),
        }[kind])

    def _has_current_acceptance(self, agreement: ProtocolNode) -> bool:
        """Whether this participant currently holds a role in this agreement.

        Taking a role is the only way to be part of an agreement, so this is
        what "accepted" now means. Deliberately local-only: it runs inside
        the ancestor walk of every guard, and what matters there is this
        session's own standing, which needs no peer lookup.
        """
        # Identity is a role - singular, and shaped differently only for that
        # reason - so holding it is being part of the agreement. Otherwise the
        # person who speaks for an agreement could be told to take a role in
        # it before they may act, which is nonsense.
        if self.holds_identity(agreement):
            return True
        mine = self._identity_uuid
        for role in self.roles(agreement):
            if not self._offer_for(role, mine):
                continue
            own = self._own_role_decision(role)
            if (
                own
                and own.data.get("decision") == "accepted"
                and not self._is_expired(own.data.get("expires_at"))
                and own.data.get("reference_hash")
                == self.role_reference_hash(agreement, role)
            ):
                return True
        return False

    def _known_people(self) -> dict[str, dict]:
        """Everyone this session can put a name to, on this topic or not."""
        return self._cached(
            ("known",),
            lambda: {
                person["uuid"]: person
                for person in self.session.known_identities()
                if person.get("uuid")
            },
        )

    def _topic_members(self, agreement_uuid: str) -> list[dict]:
        return self._cached(
            ("members", agreement_uuid),
            lambda: self._build_topic_members(agreement_uuid),
        )

    def _build_topic_members(self, agreement_uuid: str) -> list[dict]:
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
                if item.get("uuid") == self._identity_uuid
            ),
            {},
        )
        people = [{
            "uuid": self._identity_uuid,
            "name": self_identity.get("name") or "You",
            "picture": self_identity.get("picture") or "",
            "address": self.session.address,
            "addresses": [self.session.address],
            "is_self": True,
        }]
        seen = {self._identity_uuid}
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
        with self._reading():
            return self._build_organization_payload()

    def _build_organization_payload(self) -> dict:
        """Return the locally consented agreement hierarchy.

        The holding graph is a DAG - an agreement may hold seats in several
        others - but it is drawn as a tree by projecting through home: the
        first holding in order that actually reaches a root (2.5). Home edges
        are a subset of holding edges with at most one per agreement, so the
        projection is a forest for free.

        The seats home leaves out are *not* hidden. They ride along on the
        agreement as other_parents, because a second parent nobody can see is
        a trap for whoever deletes the first.

        Membership is topic-scoped: a person appears on an agreement only
        when this Session knows that peer to discuss that exact topic.
        """
        agreements = self.agreements()
        summaries = {}
        for agreement in agreements:
            parents = self.parent_payload(agreement)
            summaries[agreement.uuid] = {
                "uuid": agreement.uuid,
                "title": agreement.data.get("title") or "Untitled agreement",
                "joined": True,
                "state": self.agreement_state(agreement),
                "interaction_allowed": (
                    self._interaction_guard(agreement).status == "ok"
                ),
                "home_parent_uuid": next(
                    (
                        item["agreement_uuid"] for item in parents
                        if item["is_home"]
                    ),
                    "",
                ),
                "other_parents": [
                    {"uuid": item["agreement_uuid"], "title": item["title"]}
                    for item in parents if not item["is_home"]
                ],
                "holds_seats": bool(parents),
                "members": self._topic_members(agreement.uuid),
                "children": [],
            }

        parent_for: dict[str, str] = {}
        children_for: dict[str, list[dict]] = {
            agreement.uuid: [] for agreement in agreements
        }
        for uuid, summary in summaries.items():
            home = summary["home_parent_uuid"]
            if home and home in summaries:
                parent_for[uuid] = home
                children_for[home].append({"uuid": uuid, "joined": True})

        # Seats offered to agreements this session has not joined: their
        # topics are somebody else's to invite, so only the seat shows.
        for parent in agreements:
            for child_uuid, role in self.child_agreements(parent):
                if child_uuid in summaries or child_uuid == parent.uuid:
                    continue
                children_for[parent.uuid].append({
                    "uuid": child_uuid,
                    "title": role.data.get("name") or "Restricted subagreement",
                    "joined": False,
                    # Whose seat this is, is all that is visible from here.
                    "state": "",
                    "interaction_allowed": False,
                    "home_parent_uuid": parent.uuid,
                    "other_parents": [],
                    "holds_seats": True,
                    "members": [],
                    "children": [],
                })

        def build(uuid: str) -> dict:
            summary = dict(summaries[uuid])
            if summary["holds_seats"] and uuid not in parent_for:
                # It says it holds a seat somewhere, but no path from here
                # reaches a root, so it is drawn where it can be seen.
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
        problem = self._ancestry_problem(agreement)
        if not problem:
            return SessionResult("ok")
        kind, title = problem
        return SessionResult("error", reason={
            "cycle": "Read-only: agreement hierarchy has a cycle",
            "unjoined": "Read-only until every parent agreement is joined",
            "unseated": (
                "Read-only until the parent accepts this subagreement "
                "relationship"
            ),
            "stale": f"Read-only because {title} is not currently accepted",
        }[kind])

    def _interaction_guard_for_node(self, node_uuid: str) -> SessionResult:
        agreement = self._local_agreement_topic(node_uuid)
        if not agreement:
            return SessionResult("error", reason="agreement not found")
        return self._interaction_guard(agreement)

    def _interaction_guard_for_reaction(
        self, peer_addr: str, node_uuid: str,
    ) -> SessionResult:
        agreement = self._agreement_for_reaction(peer_addr, node_uuid)
        if not agreement:
            return SessionResult("error", reason="agreement not found")
        return self._interaction_guard(agreement)

    def _agreement_for_reaction(
        self, peer_addr: str, node_uuid: str,
    ) -> ProtocolNode | None:
        if local := self._local_agreement_topic(node_uuid):
            return local
        for topic_uuid in self.session.peer_topics_for_node(
            peer_addr, node_uuid,
        ):
            if agreement := self._node(topic_uuid, "agreement"):
                return agreement
        return None

    def _offer_authority_guard(
        self, peer_addr: str, node_uuid: str,
    ) -> SessionResult:
        """Only the Identity holder's role offers are adopted.

        A coordination rule, not a security boundary. Nothing in the protocol
        signs content, so this holds exactly as far as trusting the peers you
        chose to sync with - recorded in DESIGN_ROLES_AND_ACTORS.md rather
        than pretended away here. What it does guarantee is that every side
        reaches the same verdict, because it reads only replicated state.
        """
        node = (
            self.session.get_cached_peer_subtree(peer_addr, node_uuid)
            or self.session.protocol.index.get(node_uuid)
        )
        if not node or node.data.get("type") != "agreement_role_offer":
            return SessionResult("ok")
        agreement = self._agreement_for_reaction(peer_addr, node_uuid)
        if not agreement:
            return SessionResult("ok")
        identity = self.identity_payload(agreement)
        if identity.get("state") != "held" or identity.get("claims"):
            return SessionResult(
                "error",
                reason=(
                    "Identity here is unsettled, so role offers cannot be "
                    "adopted until it is resolved"
                ),
            )
        if node.data.get("offered_by") != identity.get("holder_actor_uuid"):
            return SessionResult(
                "error",
                reason="this role offer was not made by the Identity holder",
            )
        return SessionResult("ok")

    def _node(self, node_uuid: str | None,
              node_type: str) -> ProtocolNode | None:
        # Every lookup snapshots a subtree out of Session, and one payload
        # asks for the same agreements and roles dozens of times over. Cached
        # for the length of a read only, so a mutation always looks again -
        # which matters, because modifying a node replaces the object rather
        # than mutating it.
        if not node_uuid:
            return None
        return self._cached(
            ("node", node_uuid, node_type),
            lambda: self._lookup_node(node_uuid, node_type),
        )

    def _lookup_node(self, node_uuid: str,
                     node_type: str) -> ProtocolNode | None:
        node = self.session.protocol.index.get(node_uuid)
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
