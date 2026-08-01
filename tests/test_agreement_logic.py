import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from s_agreement.application import APPLICATION_MANIFEST
from s_agreement.logic import AgreementLogic
from sovereign import ProtocolNode, Session
from sovereign import app_server
from sovereign.relay_logic import RelayLogic


def connect(host, guest, topic_uuid: str) -> dict:
    """Wire two runtimes the way the app does: the host decides to use its
    relay for the agreement, composes an invitation, the guest accepts it."""
    host.session.start_discussion(topic_uuid)
    attached = host.mailbox_channel.attach_topics(
        [topic_uuid], {"target_id": host.relay_target},
    )
    if not attached.ok:
        return {"status": "error", "reason": attached.reason}
    identity_uuid = host.session.identity.uuid
    token = host.channel_manager.compose_token([topic_uuid], {
        topic_uuid: {
            "kind": "mailbox", "target_id": host.relay_target,
        },
        identity_uuid: {
            "kind": "mailbox", "target_id": host.relay_target,
        },
    })
    if not token.ok:
        return {"status": "error", "reason": token.reason}
    result = guest.channel_manager.accept_token(token.value)
    if not result.ok:
        return {"status": "error", "reason": result.reason}
    sync(host, guest)
    return result.value


def sync(*runtimes) -> None:
    """Move work between clients the only way a relay can: each publishes
    what changed, then each reads what the others left. Twice, because a
    client given a topic in the first round has nothing of its own to
    publish until it has grafted it."""
    for _ in range(2):
        for runtime in runtimes:
            runtime.relay.write_presence()
            runtime.relay.publish_due_topics()
        for runtime in runtimes:
            runtime.relay.poll_and_apply()


class AgreementLogicTests(unittest.TestCase):
    def test_document_snapshot_never_consults_transport_under_session(self):
        class NoTransport:
            def network_info(self, _topic_uuid=None):
                raise AssertionError("transport reached from Session snapshot")

            def peer_liveness_for_address(self, _peer, _topic_uuid=None):
                raise AssertionError("transport reached from Session snapshot")

        session = Session("local")
        logic = AgreementLogic(session, collaboration=NoTransport())
        agreement_uuid = logic.create_agreement("Atomic view").value

        with session.lock:
            snapshot = logic.document_snapshot(agreement_uuid)

        payload = logic.merge_document_observation(snapshot, {"peers": {}})
        self.assertEqual(snapshot["topic_uuid"], agreement_uuid)
        self.assertEqual(payload["network"], {"peers": {}})

    def test_document_payload_does_not_change_implicit_selection(self):
        runtime = self.runtime(8610)
        created = runtime.logic.create_agreement("Read only")
        with runtime.session.lock:
            metadata = runtime.session.application_metadata("agreement")
            metadata.pop("selected_agreement_uuid", None)

        payload = runtime.logic.document_payload()

        self.assertEqual(payload["agreement"]["uuid"], created.value)
        with runtime.session.lock:
            self.assertNotIn(
                "selected_agreement_uuid",
                runtime.session.application_metadata("agreement"),
            )

    def test_manifest_and_minimal_document_tree(self):
        runtime = self.runtime(9401)

        agreement_uuid = runtime.logic.create_agreement("Working agreement").value
        section_uuid = runtime.logic.create_section(
            agreement_uuid, "Responsibilities",
        ).value
        clause_uuid = runtime.logic.create_clause(
            section_uuid, "Each participant reviews proposed changes.",
        ).value
        payload = runtime.logic.document_payload()

        self.assertEqual(APPLICATION_MANIFEST.application_id, "agreement")
        self.assertEqual(payload["agreement"]["uuid"], agreement_uuid)
        sections = [
            child for child in payload["agreement"]["children"]
            if child["data"].get("type") == "agreement_section"
        ]
        self.assertEqual(sections[0]["uuid"], section_uuid)
        self.assertEqual(sections[0]["children"][0]["uuid"], clause_uuid)

    def test_acceptance_is_a_separate_hashed_timestamped_item(self):
        runtime = self.runtime(9458)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        agreement = runtime.session.protocol.index[agreement_uuid]
        role = runtime.logic.roles(agreement)[0]
        decisions = [
            child for child in role.live_children()
            if child.data.get("type") == "agreement_role_decision"
        ]

        self.assertEqual(len(decisions), 1)
        decision = decisions[0].data
        self.assertEqual(
            decision["actor_uuid"], runtime.session.identity.uuid,
        )
        self.assertEqual(decision["decision"], "accepted")
        self.assertTrue(decision["decided_at"].endswith("Z"))
        self.assertTrue(decision["reference_hash"].startswith("sha256:"))
        self.assertIsNone(decision["expires_at"])
        # Offers and answers have their own storage nodes, but they are
        # records about the agreement rather than content of it, so they
        # stay out of the document serialization.
        serialized = runtime.logic.document_payload(
            agreement_uuid,
        )["agreement"]["children"]
        role_view = next(
            child for child in serialized
            if child["data"].get("type") == "agreement_role"
        )
        self.assertEqual(
            {child["data"].get("type") for child in role_view["children"]},
            set(),
        )

    def test_refusal_updates_the_users_item_and_renders_a_badge(self):
        runtime = self.runtime(9459)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        agreement = runtime.session.protocol.index[agreement_uuid]
        role = runtime.logic.roles(agreement)[0]
        original = next(
            child for child in role.live_children()
            if child.data.get("type") == "agreement_role_decision"
        )

        result = runtime.logic.decide_role(
            role.uuid, "refused", "2035-01-01T00:00:00Z",
        )

        self.assertEqual(result.status, "ok")
        role = runtime.session.protocol.index[role.uuid]
        decisions = [
            child for child in role.live_children()
            if child.data.get("type") == "agreement_role_decision"
        ]
        # Answering again rewrites the one record rather than stacking.
        self.assertEqual([item.uuid for item in decisions], [original.uuid])
        holder = runtime.logic.role_holders(agreement, role)[0]
        self.assertEqual(holder["status"], "refused")
        self.assertEqual(holder["expires_at"], "2035-01-01T00:00:00Z")
        # Refusing every role held here, Identity included, is how
        # somebody steps out of the agreement altogether. Refetched
        # because modifying a node replaces the object rather than
        # mutating it.
        runtime.logic.offer_identity(agreement_uuid, "somebody-else")
        self.assertFalse(
            runtime.logic._has_current_acceptance(
                runtime.session.protocol.index[agreement_uuid],
            ),
        )

    def test_content_change_makes_acceptance_outdated_until_renewed(self):
        runtime = self.runtime(9464)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        section_uuid = runtime.logic.create_section(
            agreement_uuid, "Purpose",
        ).value

        self.assertEqual(
            self.own_standing(runtime, agreement_uuid), "outdated",
        )

        self.rejoin(runtime, agreement_uuid)
        self.assertEqual(
            self.own_standing(runtime, agreement_uuid), "accepted",
        )
        agreement = runtime.session.protocol.index[agreement_uuid]
        role = runtime.logic.roles(agreement)[0]
        own = runtime.logic._own_role_decision(role)
        self.assertEqual(
            own.data["reference_hash"],
            runtime.logic.role_reference_hash(agreement, role),
        )
        runtime.logic.create_clause(section_uuid, "Serve the members.")
        self.assertEqual(
            self.own_standing(runtime, agreement_uuid), "outdated",
        )

    def test_every_ancestor_requires_a_current_acceptance(self):
        runtime = self.runtime(9465)
        root_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_subagreement(
            root_uuid, "Operations",
        ).value
        self.leave(runtime, root_uuid)

        blocked = runtime.logic.create_subagreement(child_uuid, "Purchasing")

        self.assertEqual(blocked.status, "error")
        # Only the root was left, so the root is what blocks - the level
        # between was never written to.
        self.assertIn("Cooperative", blocked.reason)
        self.rejoin(runtime, root_uuid)
        self.rejoin(runtime, child_uuid)
        allowed = runtime.logic.create_subagreement(
            child_uuid, "Purchasing",
        )
        self.assertEqual(allowed.status, "ok")

    def test_expired_parent_acceptance_blocks_a_subagreement(self):
        runtime = self.runtime(9466)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        agreement = runtime.session.protocol.index[parent_uuid]
        for role in runtime.logic.roles(agreement):
            runtime.logic.decide_role(
                role.uuid, "accepted", "2000-01-01T00:00:00Z",
            )
        # Identity would otherwise keep this session in the agreement
        # regardless of the lapsed role.
        runtime.logic.offer_identity(parent_uuid, "somebody-else")

        blocked = runtime.logic.create_subagreement(
            parent_uuid, "Operations",
        )

        self.assertEqual(blocked.status, "error")
        self.assertEqual(self.own_standing(runtime, parent_uuid), "expired")

    def test_leaving_a_parent_closes_its_descendants_without_writing_to_them(self):
        # Invalidity is derived, never recorded. The cascade this replaces
        # wrote a refusal into every descendant, which destroyed the
        # participant's own answers there and never undid itself when the
        # parent was taken up again.
        runtime = self.runtime(9467)
        root_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_subagreement(
            root_uuid, "Operations",
        ).value
        grandchild_uuid = runtime.logic.create_subagreement(
            child_uuid, "Purchasing",
        ).value

        self.leave(runtime, root_uuid)

        def writable(agreement_uuid):
            return runtime.logic.interaction_payload(
                runtime.session.protocol.index[agreement_uuid],
            )["allowed"]

        # A root has no ancestor to be blocked by, so it stays writable; its
        # descendants do not.
        self.assertTrue(writable(root_uuid))
        self.assertFalse(writable(child_uuid))
        self.assertFalse(writable(grandchild_uuid))
        # Nothing was written into them, so the answers held there survive.
        for agreement_uuid in (child_uuid, grandchild_uuid):
            self.assertEqual(
                self.own_standing(runtime, agreement_uuid), "accepted",
            )
        self.assertEqual(
            [
                item.data["title"]
                for item in runtime.logic.descendant_agreements(root_uuid)
            ],
            ["Operations", "Purchasing"],
        )

        # And because nothing was written, taking the root back up restores
        # the whole subtree at once rather than one level at a time.
        self.rejoin(runtime, root_uuid)
        self.assertTrue(writable(child_uuid))
        self.assertTrue(writable(grandchild_uuid))
    def test_blocked_subagreement_is_visible_but_all_mutations_are_rejected(self):
        runtime = self.runtime(9468)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_subagreement(
            parent_uuid, "Operations",
        ).value
        section_uuid = runtime.logic.create_section(
            child_uuid, "Responsibilities",
        ).value
        self.leave(runtime, parent_uuid)

        selected = runtime.logic.select_agreement(child_uuid)
        payload = runtime.logic.document_payload(child_uuid)

        self.assertEqual(selected.status, "ok")
        self.assertEqual(payload["agreement"]["uuid"], child_uuid)
        self.assertFalse(payload["interaction"]["allowed"])
        self.assertIn("Cooperative", payload["interaction"]["reason"])
        for result in (
            runtime.logic.rename_agreement(child_uuid, "Changed"),
            runtime.logic.create_section(child_uuid, "Blocked"),
            runtime.logic.rename_section(section_uuid, "Changed"),
            runtime.logic.delete_agreement(child_uuid),
        ):
            self.assertEqual(result.status, "error")
            self.assertIn("Read-only", result.reason)

    def test_subagreement_is_linked_but_remains_an_independent_topic(self):
        runtime = self.runtime(9460)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value

        child_uuid = runtime.logic.create_subagreement(
            parent_uuid, "Finance circle",
        ).value

        parent = runtime.session.protocol.index[parent_uuid]
        child = runtime.session.protocol.index[child_uuid]
        # A subagreement is an Agreement holding a role in its parent, so
        # the parent side is an ordinary role offered to an Agreement
        # actor and the child side names that same seat.
        seats = runtime.logic.child_agreements(parent)
        self.assertEqual(child.parent_uuid, parent.parent_uuid)
        self.assertEqual(len(seats), 1)
        seated_uuid, role = seats[0]
        self.assertEqual(seated_uuid, child_uuid)
        self.assertEqual(role.data["name"], "Finance circle")
        holdings = runtime.logic.parent_holdings(child)
        self.assertEqual(len(holdings), 1)
        self.assertEqual(
            holdings[0].data["parent_agreement_uuid"], parent_uuid,
        )
        self.assertEqual(holdings[0].data["role_uuid"], role.uuid)
        self.assertEqual(
            {item.uuid for item in runtime.logic.agreements()},
            {parent_uuid, child_uuid},
        )

        organization = runtime.logic.organization_payload()
        parent_view = next(
            item for item in organization["roots"]
            if item["uuid"] == parent_uuid
        )
        self.assertEqual(
            [item["uuid"] for item in parent_view["children"]],
            [child_uuid],
        )

    def test_joining_parent_does_not_join_its_subagreement(self):
        left, right = self.runtime(9461), self.runtime(9462)
        parent_uuid = left.logic.create_agreement("Cooperative").value
        self.assertEqual(connect(left, right, parent_uuid)["status"], "ok")

        # Joining the topic is not joining the agreement. Until a role is
        # held, somebody is present and nothing more.
        people = right.logic.participants(parent_uuid)
        self.assertEqual(len(people), 2)
        mine = next(person for person in people if person["is_self"])
        self.assertTrue(mine["is_observer"])
        self.assertEqual(mine["roles"], [])
        self.assertTrue(all("name" in person for person in people))
        self.assertTrue(all("picture" in person for person in people))

        # Ask, and have it confirmed.
        participant_uuid = right.logic.roles(
            right.session.protocol.index[parent_uuid],
        )[0].uuid
        right.logic.decide_role(participant_uuid, "accepted")
        sync(left, right)
        left.logic.offer_role(participant_uuid, right.session.identity.uuid)
        sync(left, right)
        # The confirmation is a proposal until taken up, because nothing here
        # merges a peer's node on its own. Answering again takes it up.
        agreement = right.session.protocol.index[parent_uuid]
        role = right.session.protocol.index[participant_uuid]
        asked = next(
            holder for holder in right.logic.role_holders(agreement, role)
            if holder["is_self"]
        )
        self.assertEqual(asked["status"], "requested")
        self.assertTrue(asked["confirmed_elsewhere"])
        right.logic.decide_role(participant_uuid, "accepted")
        sync(left, right)
        self.assertEqual(self.own_standing(right, parent_uuid), "accepted")

        child_uuid = left.logic.create_subagreement(
            parent_uuid, "Finance circle",
        ).value
        link_uuid = left.logic.child_agreements(
            left.session.protocol.index[parent_uuid],
        )[0][1].uuid
        sync(left, right)

        right_agreements = {item.uuid for item in right.logic.agreements()}
        self.assertIn(parent_uuid, right_agreements)
        self.assertNotIn(child_uuid, right_agreements)
        payload = right.logic.document_payload(parent_uuid)
        self.assertIn(
            link_uuid,
            {entry["node"]["uuid"] for entry in payload["proposed_nodes"]},
        )
        self.assertEqual(
            right.logic.organization_payload()["roots"][0]["children"], [],
        )

        # Agreeing to the parent-side relationship makes the unit visible,
        # but still does not mount the separately shared child topic.
        accepted = right.logic.accept_peer_node(left.peer_addr, link_uuid)
        self.assertEqual(accepted.status, "ok")
        parent_view = right.logic.organization_payload()["roots"][0]
        restricted = parent_view["children"][0]
        self.assertEqual(restricted["uuid"], child_uuid)
        self.assertFalse(restricted["joined"])

        # Taking up the seat does not disturb anybody's acceptance of the
        # parent. A subagreement seat is a role, and a role is outside the
        # document body, so adding a subunit changes nothing that anyone
        # agreed to - unlike the link it replaces, which forced everyone to
        # re-accept the parent whenever the organisation grew.
        self.assertEqual(self.own_standing(right, parent_uuid), "accepted")

        # The invitation is still needed, and it mounts only because a role
        # is held in the parent.
        self.assertEqual(connect(left, right, child_uuid)["status"], "ok")
        right.session.mount_cached_topics("agreement")
        sync(left, right)
        self.assertIn(
            child_uuid, {item.uuid for item in right.logic.agreements()},
        )
        parent_view = next(
            item for item in right.logic.organization_payload()["roots"]
            if item["uuid"] == parent_uuid
        )
        self.assertTrue(parent_view["children"][0]["joined"])

    def test_a_subagreement_stays_unmounted_while_no_role_is_held_above_it(self):
        # The mounting rule the test above relies on, on its own: an
        # invitation to a subagreement is cached rather than mounted until
        # this session holds something in every agreement above it.
        left, right = self.runtime(9512), self.runtime(9513)
        parent_uuid = left.logic.create_agreement("Cooperative").value
        child_uuid = left.logic.create_subagreement(
            parent_uuid, "Finance circle",
        ).value
        self.assertEqual(connect(left, right, parent_uuid)["status"], "ok")
        right.logic.accept_agreement_invitation(
            right.session.protocol.index[parent_uuid],
        )
        sync(left, right)

        # Present in the parent, holding nothing in it.
        self.assertEqual(connect(left, right, child_uuid)["status"], "ok")
        right.session.mount_cached_topics("agreement")
        self.assertNotIn(
            child_uuid, {item.uuid for item in right.logic.agreements()},
        )

        # Asking and being confirmed is what opens it.
        participant_uuid = right.logic.roles(
            right.session.protocol.index[parent_uuid],
        )[0].uuid
        right.logic.decide_role(participant_uuid, "accepted")
        sync(left, right)
        left.logic.offer_role(participant_uuid, right.session.identity.uuid)
        sync(left, right)
        right.logic.decide_role(participant_uuid, "accepted")
        sync(left, right)
        right.session.mount_cached_topics("agreement")
        self.assertIn(
            child_uuid, {item.uuid for item in right.logic.agreements()},
        )
    def test_deleting_parent_promotes_child_instead_of_deleting_it(self):
        runtime = self.runtime(9463)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_subagreement(
            parent_uuid, "Finance circle",
        ).value

        result = runtime.logic.delete_agreement(parent_uuid)

        self.assertEqual(result.status, "ok")
        child = runtime.session.protocol.index[child_uuid]
        self.assertFalse(child.deleted)
        self.assertNotIn("parent_agreement_uuid", child.data)
        self.assertEqual(
            [item["uuid"] for item in runtime.logic.organization_payload()["roots"]],
            [child_uuid],
        )

    def test_deleting_a_seated_child_empties_its_seat_and_no_more(self):
        # A role is not a subagreement's private property: the same one may
        # seat several actors. Deleting the agreement in it must remove its
        # answer, not the role everybody else is holding too.
        runtime = self.runtime(9524)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        role_uuid = runtime.logic.create_role(parent_uuid, "Delegate").value
        runtime.logic.offer_role(role_uuid, "somebody-else")
        child_uuid = runtime.logic.create_seated_agreement(
            role_uuid, "Finance circle",
        ).value

        self.assertEqual(
            runtime.logic.delete_agreement(child_uuid).status, "ok",
        )

        parent = runtime.session.protocol.index[parent_uuid]
        role = runtime.session.protocol.index[role_uuid]
        self.assertIn(
            "Delegate",
            [item.data["name"] for item in runtime.logic.roles(parent)],
        )
        self.assertFalse(runtime.logic._agreement_holds_role(role, child_uuid))
        # The other holder is untouched, and the seat itself stays offered:
        # revoking an offer is the parent's to do, not the departing child's.
        holders = {
            holder["actor_uuid"]: holder
            for holder in runtime.logic.role_holders(parent, role)
        }
        self.assertIn("somebody-else", holders)
        self.assertNotEqual(holders[child_uuid]["status"], "accepted")

    def test_reacting_resolves_a_divergence_on_a_clause(self):
        # Without reactions an agreement can reach a state it cannot leave:
        # two sides edit the same clause, both see divergence, and nothing
        # either does resolves it. This is that dead end, and its exit.
        left, right = self.runtime(9410), self.runtime(9411)
        agreement_uuid = left.logic.create_agreement("Service terms").value
        section_uuid = left.logic.create_section(agreement_uuid, "Scope").value
        clause_uuid = left.logic.create_clause(section_uuid, "Original text.").value
        connect(left, right, agreement_uuid)

        # Both sides rewrite the same clause without seeing the other's edit.
        left.logic.update_clause(clause_uuid, "Left text.")
        right.logic.update_clause(clause_uuid, "Right text.")
        sync(left, right)
        grouped = right.logic.document_payload(agreement_uuid)["transition_by_node"]
        self.assertEqual(grouped[clause_uuid]["type"], "divergence")
        self.assertIn(grouped[clause_uuid]["reaction"], {"adopt", "rollback"})

        # Reacting with adopt takes the peer's revision and leaves the
        # divergence behind - the exit that did not exist before.
        self.assertEqual(
            right.logic.accept_peer_node(left.peer_addr, clause_uuid).status, "ok",
        )
        self.assertEqual(
            right.session.protocol.index[clause_uuid].data["text"], "Left text.",
        )
        settled = right.logic.document_payload(agreement_uuid)["transition_by_node"]
        self.assertNotEqual(settled.get(clause_uuid, {}).get("type"), "divergence")

    def test_reacting_refuses_a_node_outside_this_application(self):
        runtime = self.runtime(9412)
        runtime.logic.create_agreement("Service terms")
        foreign = runtime.session.create_child(
            runtime.session.protocol.root.uuid, {"type": "not_an_agreement"}, {},
        ).value

        result = runtime.logic.accept_peer_node("http://peer", foreign.uuid)
        self.assertEqual(result.status, "error")
        self.assertEqual(
            runtime.logic.rollback_peer_node("http://peer", foreign.uuid).status,
            "error",
        )

    def test_transition_priority_comes_from_session_not_per_application(self):
        # Applications grouping transition events per node had each copied
        # Session's ranking, and the copies drifted, so the same conflict
        # could surface as divergence in one application and something milder
        # in another.
        #
        # Only this application is checked here. Core ships these tests, and a
        # Core test that imports S-Kanban cannot run for anyone who installed
        # Core alone - which is the dependency direction the architecture
        # forbids in the first place. The cross-application comparison lives
        # in test_cross_application.py, which stays in the working repository
        # where every application is present.
        from s_agreement import logic as agreement_logic

        source = Path(agreement_logic.__file__).read_text(encoding="utf-8")
        self.assertIn("Session.TRANSITION_PRIORITY", source)
        self.assertNotIn('"divergence": 5', source)
        self.assertNotIn('"divergence": 6', source)

        priority = Session.TRANSITION_PRIORITY
        self.assertGreater(priority["divergence"], priority["peer_made_changes"])
        self.assertGreater(priority["peer_made_changes"], priority["in_transition"])
        self.assertEqual(priority["in_agreement"], 0)

    def test_titles_and_text_stay_editable_after_creation(self):
        runtime = self.runtime(9403)
        agreement_uuid = runtime.logic.create_agreement("Draft").value
        section_uuid = runtime.logic.create_section(agreement_uuid, "Scpoe").value
        clause_uuid = runtime.logic.create_clause(section_uuid, "Frist draft.").value

        self.assertEqual(
            runtime.logic.rename_agreement(agreement_uuid, "Service terms").status, "ok",
        )
        self.assertEqual(
            runtime.logic.rename_section(section_uuid, "Scope").status, "ok",
        )
        self.assertEqual(
            runtime.logic.update_clause(clause_uuid, "First draft.").status, "ok",
        )

        payload = runtime.logic.document_payload()
        section = next(
            child for child in payload["agreement"]["children"]
            if child["data"].get("type") == "agreement_section"
        )
        self.assertEqual(payload["agreement"]["data"]["title"], "Service terms")
        self.assertEqual(section["data"]["title"], "Scope")
        self.assertEqual(section["children"][0]["data"]["text"], "First draft.")

    def test_renaming_rejects_blank_titles_and_unknown_nodes(self):
        runtime = self.runtime(9404)
        agreement_uuid = runtime.logic.create_agreement("Draft").value
        section_uuid = runtime.logic.create_section(agreement_uuid, "Scope").value

        self.assertEqual(runtime.logic.rename_agreement(agreement_uuid, "  ").status, "error")
        self.assertEqual(runtime.logic.rename_section(section_uuid, "").status, "error")
        self.assertEqual(runtime.logic.rename_section("missing", "Scope").status, "error")
        # A section uuid is not an agreement uuid; the type guard must hold.
        self.assertEqual(runtime.logic.rename_agreement(section_uuid, "Nope").status, "error")

    def test_sections_and_clauses_can_be_reordered(self):
        runtime = self.runtime(9414)
        agreement_uuid = runtime.logic.create_agreement("Terms").value
        first = runtime.logic.create_section(agreement_uuid, "First").value
        second = runtime.logic.create_section(agreement_uuid, "Second").value
        third = runtime.logic.create_section(agreement_uuid, "Third").value

        def section_titles():
            payload = runtime.logic.document_payload(agreement_uuid)
            live = [
                s for s in payload["agreement"]["children"]
                if not s["deleted"]
                and s["data"].get("type") == "agreement_section"
            ]
            ordered = sorted(live, key=lambda s: s["data"].get("order", 0))
            return [s["data"]["title"] for s in ordered]

        self.assertEqual(section_titles(), ["First", "Second", "Third"])
        # Move "Third" to the front.
        self.assertEqual(runtime.logic.move_section(third, 0).status, "ok")
        self.assertEqual(section_titles(), ["Third", "First", "Second"])
        # Move "Third" back down one.
        self.assertEqual(runtime.logic.move_section(third, 1).status, "ok")
        self.assertEqual(section_titles(), ["First", "Third", "Second"])

        a = runtime.logic.create_clause(first, "Clause A").value
        b = runtime.logic.create_clause(first, "Clause B").value

        def clause_texts():
            payload = runtime.logic.document_payload(agreement_uuid)
            section = next(s for s in payload["agreement"]["children"] if s["uuid"] == first)
            live = [c for c in section["children"] if not c["deleted"]]
            ordered = sorted(live, key=lambda c: c["data"].get("order", 0))
            return [c["data"]["text"] for c in ordered]

        self.assertEqual(clause_texts(), ["Clause A", "Clause B"])
        self.assertEqual(runtime.logic.move_clause(b, 0).status, "ok")
        self.assertEqual(clause_texts(), ["Clause B", "Clause A"])

    def test_agenda_items_can_be_reordered(self):
        runtime = self.runtime(9416)
        agreement_uuid = runtime.logic.create_agreement("Terms").value
        first = runtime.logic.create_agenda_item(
            agreement_uuid, "First topic",
        ).value
        second = runtime.logic.create_agenda_item(
            agreement_uuid, "Second topic",
        ).value

        result = runtime.logic.move_agenda_item(second.uuid, 0)

        self.assertEqual(result.status, "ok")
        self.assertEqual(
            [item.uuid for item in runtime.session.agenda_items(agreement_uuid)],
            [second.uuid, first.uuid],
        )

    def test_move_rejects_wrong_node_types(self):
        runtime = self.runtime(9415)
        agreement_uuid = runtime.logic.create_agreement("Terms").value
        section_uuid = runtime.logic.create_section(agreement_uuid, "S").value
        clause_uuid = runtime.logic.create_clause(section_uuid, "C").value
        # A clause is not a section and vice versa; the guards must hold.
        self.assertEqual(runtime.logic.move_section(clause_uuid, 0).status, "error")
        self.assertEqual(runtime.logic.move_clause(section_uuid, 0).status, "error")

    def test_deleting_a_section_removes_its_clauses(self):
        runtime = self.runtime(9405)
        agreement_uuid = runtime.logic.create_agreement("Draft").value
        kept_uuid = runtime.logic.create_section(agreement_uuid, "Kept").value
        removed_uuid = runtime.logic.create_section(agreement_uuid, "Removed").value
        clause_uuid = runtime.logic.create_clause(removed_uuid, "Goes away.").value
        runtime.logic.create_clause(kept_uuid, "Stays.")

        self.assertEqual(runtime.logic.delete_section(removed_uuid).status, "ok")

        payload = runtime.logic.document_payload()
        sections = payload["agreement"]["children"]
        live = [
            item for item in sections
            if not item["deleted"]
            and item["data"].get("type") == "agreement_section"
        ]
        self.assertEqual([item["uuid"] for item in live], [kept_uuid])
        # Deleting a container prunes its descendants out of the index rather
        # than tombstoning each one, so the clause is gone, not flagged.
        self.assertNotIn(clause_uuid, runtime.session.protocol.index)

    def test_deleting_a_single_clause_leaves_its_siblings(self):
        runtime = self.runtime(9406)
        agreement_uuid = runtime.logic.create_agreement("Draft").value
        section_uuid = runtime.logic.create_section(agreement_uuid, "Scope").value
        first_uuid = runtime.logic.create_clause(section_uuid, "First.").value
        second_uuid = runtime.logic.create_clause(section_uuid, "Second.").value

        self.assertEqual(runtime.logic.delete_clause(first_uuid).status, "ok")

        payload = runtime.logic.document_payload()
        clauses = next(
            child for child in payload["agreement"]["children"]
            if child["data"].get("type") == "agreement_section"
        )["children"]
        live = [item["uuid"] for item in clauses if not item["deleted"]]
        self.assertEqual(live, [second_uuid])

    def test_document_payload_does_not_expose_channel_management(self):
        runtime = self.runtime(9402)
        runtime.logic.create_agreement("Service terms")

        payload = runtime.logic.document_payload()
        self.assertNotIn("channel_targets", payload)
        self.assertNotIn("channel_target_id", payload)

    def test_agreement_has_no_automatic_adoption_surface(self):
        runtime = self.runtime(9412)
        runtime.logic.create_agreement("Manual decisions")

        payload = runtime.logic.document_payload()
        self.assertNotIn("auto_adopt_mode", payload)
        self.assertNotIn("auto_adopt_modes", payload)
        paths = {route.path for route in app_server.build_app(runtime).routes}
        self.assertNotIn("/api/agreement/auto_adopt", paths)

    def test_invitation_and_transition_visibility(self):
        left = self.runtime(9402)
        right = self.runtime(9403)
        agreement_uuid = left.logic.create_agreement("Shared agreement").value
        section_uuid = left.logic.create_section(agreement_uuid, "Scope").value
        clause_uuid = left.logic.create_clause(section_uuid, "Initial text").value

        accepted = connect(left, right, agreement_uuid)

        self.assertEqual(accepted["status"], "ok")
        self.assertIn(agreement_uuid, [item.uuid for item in right.logic.agreements()])
        left.logic.update_clause(clause_uuid, "Proposed replacement")
        sync(left, right)
        events = right.logic.transition_events(agreement_uuid)
        clause_events = [event for event in events if event["node_uuid"] == clause_uuid]
        self.assertEqual(len(clause_events), 1)
        self.assertIn(clause_events[0]["type"], {"peer_made_changes", "in_transition"})

    def test_three_level_new_structure_adopts_in_one_pass_child_first(self):
        left = self.runtime(9404)
        right = self.runtime(9405)
        agreement_uuid = left.logic.create_agreement("Nested agreement").value
        accepted = connect(left, right, agreement_uuid)
        self.assertEqual(accepted["status"], "ok")

        section_uuid = left.logic.create_section(agreement_uuid, "New section").value
        clause_uuid = left.logic.create_clause(section_uuid, "Nested clause").value
        sync(left, right)
        proposals = {
            entry["node"]["uuid"]: entry
            for entry in right.logic.document_payload(
                agreement_uuid,
            )["proposed_nodes"]
        }
        self.assertIn(section_uuid, proposals)
        self.assertIn(clause_uuid, proposals)
        self.assertEqual(
            proposals[section_uuid]["node"]["data"]["title"], "New section",
        )
        events = right.session.analyze_peer_transitions(
            left.peer_addr, agreement_uuid,
        )
        incoming = [
            event for event in events
            if event["node_uuid"] in {section_uuid, clause_uuid}
        ]
        child_first = sorted(
            incoming, key=lambda event: event["node_uuid"] != clause_uuid,
        )

        with patch.object(
            right.session, "analyze_peer_transitions", return_value=child_first,
        ):
            adopted = right.logic.adopt_peer_changes(
                left.peer_addr, agreement_uuid,
            )

        self.assertEqual(adopted.status, "ok")
        self.assertTrue(adopted.value)
        self.assertIn(section_uuid, right.session.protocol.index)
        self.assertIn(clause_uuid, right.session.protocol.index)
        self.assertEqual(
            right.session.protocol.index[clause_uuid].parent_uuid, section_uuid,
        )

    def test_mailbox_invitation_mounts_agreement_without_core_special_case(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            session_a = Session("addr-a")
            logic_a = AgreementLogic(session_a)
            session_a.register_application(logic_a.application_registration())
            agreement_uuid = logic_a.create_agreement("Relayed agreement").value
            section_uuid = logic_a.create_section(agreement_uuid, "Scope").value
            clause_uuid = logic_a.create_clause(section_uuid, "Mailbox clause").value
            relay_a = RelayLogic(
                session_a, self.relay_config(relay_root, "A", state_dir),
            )
            relay_a.mark_topics_shared([agreement_uuid])
            relay_a.publish_due_topics()
            descriptor = relay_a.channel_descriptor()

            session_b = Session("addr-b")
            logic_b = AgreementLogic(session_b)
            session_b.register_application(logic_b.application_registration())
            relay_b = RelayLogic(
                session_b,
                {"relay_state_file": str(Path(state_dir) / "state-B.json")},
            )
            self.assertTrue(relay_b.adopt_storage_from_descriptor(descriptor))
            relay_b.mark_topics_desired([agreement_uuid])

            applied = relay_b.poll_and_apply()

            self.assertIn((agreement_uuid, "A"), applied)
            self.assertIn(agreement_uuid, [item.uuid for item in logic_b.agreements()])
            self.assertIn(clause_uuid, session_b.protocol.index)

            updated = logic_a.update_clause(clause_uuid, "Updated through mailbox")
            self.assertEqual(updated.status, "ok")
            relay_a.publish_due_topics()
            self.assertIn((agreement_uuid, "A"), relay_b.poll_and_apply())
            events = logic_b.transition_events(agreement_uuid)
            self.assertTrue(any(
                event["node_uuid"] == clause_uuid
                and event["type"] != "in_agreement"
                for event in events
            ))
            adopted = logic_b.adopt_peer_changes("relay:A", agreement_uuid)
            self.assertTrue(adopted.value)
            self.assertEqual(
                session_b.protocol.index[clause_uuid].data["text"],
                "Updated through mailbox",
            )

    def test_delete_agreement_removes_the_whole_document(self):
        runtime = self.runtime(9451)
        logic: AgreementLogic = runtime.logic
        agreement_uuid = logic.create_agreement("Working agreement").value
        section_uuid = logic.create_section(agreement_uuid, "Scope").value
        clause_uuid = logic.create_clause(section_uuid, "One clause.").value

        result = logic.delete_agreement(agreement_uuid)

        self.assertEqual(result.status, "ok")
        self.assertEqual(logic.agreements(), [])
        for node_uuid in (agreement_uuid, section_uuid, clause_uuid):
            node = runtime.session.protocol.index.get(node_uuid)
            self.assertTrue(node is None or node.deleted, node_uuid)

    def test_deleting_the_last_agreement_leaves_none_selected(self):
        runtime = self.runtime(9452)
        logic: AgreementLogic = runtime.logic
        agreement_uuid = logic.create_agreement("Working agreement").value

        logic.delete_agreement(agreement_uuid)

        self.assertIsNone(logic.document_payload()["agreement"])

    def test_delete_agreement_rejects_a_node_that_is_not_one(self):
        runtime = self.runtime(9453)
        logic: AgreementLogic = runtime.logic
        agreement_uuid = logic.create_agreement("Working agreement").value
        section_uuid = logic.create_section(agreement_uuid, "Scope").value

        result = logic.delete_agreement(section_uuid)

        self.assertEqual(result.status, "error")
        self.assertEqual(len(logic.agreements()), 1)

    def test_sections_and_clauses_are_returned_in_display_order(self):
        # Personal Cockpit reads an agreement through these, so the order
        # they return is the order the document is read in.
        runtime = self.runtime(9454)
        logic: AgreementLogic = runtime.logic
        agreement_uuid = logic.create_agreement("Working agreement").value
        first = logic.create_section(agreement_uuid, "First").value
        second = logic.create_section(agreement_uuid, "Second").value
        logic.create_clause(first, "Clause one.")
        logic.create_clause(first, "Clause two.")
        logic.move_section(second, 0)

        agreement = runtime.session.protocol.index[agreement_uuid]
        sections = logic.sections(agreement)

        self.assertEqual(
            [node.data["title"] for node in sections], ["Second", "First"],
        )
        self.assertEqual(
            [node.data["text"] for node in logic.clauses(sections[1])],
            ["Clause one.", "Clause two."],
        )

    def test_a_new_agreement_starts_with_its_creator_participating(self):
        runtime = self.runtime(9497)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        agreement = runtime.session.protocol.index[agreement_uuid]

        role = runtime.logic.roles(agreement)[0]
        self.assertEqual(role.data["name"], "Participant")
        holders = runtime.logic.role_holders(agreement, role)
        self.assertEqual(len(holders), 1)
        self.assertTrue(holders[0]["is_self"])
        self.assertEqual(holders[0]["status"], "accepted")
        # One actor: an instantiated template. Nothing is useful yet, but
        # taking part does not require inventing a role first.
        self.assertEqual(
            role.data["purpose"], "Take part in this agreement",
        )

    def test_revoking_removes_the_offer_and_leaves_their_own_record(self):
        # The authorship rule: each side may withdraw what it wrote, and
        # neither may delete what the other wrote. A holding is live only
        # while both records are present, so either withdrawal ends it.
        left, right = self.runtime(9498), self.runtime(9499)
        agreement_uuid = left.logic.create_agreement("Charter").value
        agreement = left.session.protocol.index[agreement_uuid]
        role_uuid = left.logic.create_role(agreement_uuid, "Treasurer").value
        connect(left, right, agreement_uuid)
        right.logic.accept_agreement_invitation(
            right.session.protocol.index[agreement_uuid],
        )
        sync(left, right)
        left.logic.offer_role(role_uuid, right.session.identity.uuid)
        sync(left, right)
        self.assertEqual(
            right.logic.decide_role(role_uuid, "accepted").status, "ok",
        )
        sync(left, right)

        self.assertEqual(
            left.logic.revoke_role_offer(
                role_uuid, right.session.identity.uuid,
            ).status,
            "ok",
        )
        role = left.session.protocol.index[role_uuid]
        self.assertEqual(left.logic.role_offers(role), [])
        # Their decision is theirs. It survives, pointing at nothing.
        theirs = right.session.protocol.index[role_uuid]
        self.assertTrue(right.logic._own_role_decision(theirs))
        # The role is no longer held. The leftover answer reads as
        # revoked rather than as a fresh request, or the Identity holder
        # would be asked to re-offer what they had just withdrawn.
        remaining = left.logic.role_holders(agreement, role)
        self.assertEqual([item["status"] for item in remaining], ["revoked"])

    def test_resigning_removes_only_the_participants_own_record(self):
        runtime = self.runtime(9500)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        agreement = runtime.session.protocol.index[agreement_uuid]
        role = runtime.logic.roles(agreement)[0]

        self.assertEqual(runtime.logic.resign_role(role.uuid).status, "ok")
        role = runtime.session.protocol.index[role.uuid]
        self.assertIsNone(runtime.logic._own_role_decision(role))
        # The offer was the agreement's to write, so resigning leaves it -
        # the seat stays open rather than disappearing.
        self.assertEqual(len(runtime.logic.role_offers(role)), 1)
        self.assertEqual(
            runtime.logic.role_holders(agreement, role)[0]["status"], "pending",
        )

    def test_only_identity_offers_but_anyone_may_ask_for_a_role(self):
        left, right = self.runtime(9501), self.runtime(9502)
        agreement_uuid = left.logic.create_agreement("Charter").value
        role_uuid = left.logic.create_role(agreement_uuid, "Treasurer").value
        connect(left, right, agreement_uuid)
        right.logic.accept_agreement_invitation(
            right.session.protocol.index[agreement_uuid],
        )
        sync(left, right)

        refused = right.logic.offer_role(role_uuid, right.session.identity.uuid)
        self.assertEqual(refused.status, "error")
        self.assertIn("only the Identity holder", refused.reason)
        self.assertEqual(
            right.logic.revoke_role_offer(
                role_uuid, right.session.identity.uuid,
            ).status,
            "error",
        )
        # Answering without an offer is not refused - it is how somebody
        # who holds nothing asks for their first role, since only the
        # Identity holder can offer and a newcomer cannot reach them
        # any other way.
        asked = right.logic.decide_role(role_uuid, "accepted")
        self.assertEqual(asked.status, "ok")
        role = right.session.protocol.index[role_uuid]
        agreement = right.session.protocol.index[agreement_uuid]
        requested = right.logic.role_holders(agreement, role)
        self.assertEqual(
            [item["status"] for item in requested], ["requested"],
        )

    def test_a_newcomer_asks_and_the_identity_holder_confirms(self):
        # How somebody who holds nothing gets their first role. Only the
        # Identity holder can offer, so a joiner cannot be let in by anyone
        # else; asking is the move available to them, and confirming is the
        # move available to Identity. Neither side writes the other's record.
        left, right = self.runtime(9509), self.runtime(9510)
        agreement_uuid = left.logic.create_agreement("Charter").value
        agreement = left.session.protocol.index[agreement_uuid]
        connect(left, right, agreement_uuid)
        right.logic.accept_agreement_invitation(
            right.session.protocol.index[agreement_uuid],
        )
        sync(left, right)
        participant_uuid = right.logic.roles(
            right.session.protocol.index[agreement_uuid],
        )[0].uuid

        # Joined, but holding nothing yet.
        role = left.session.protocol.index[participant_uuid]
        self.assertEqual(
            [holder["is_self"]
             for holder in left.logic.role_holders(agreement, role)],
            [True],
        )

        self.assertEqual(
            right.logic.decide_role(participant_uuid, "accepted").status, "ok",
        )
        sync(left, right)

        def theirs():
            role = left.session.protocol.index[participant_uuid]
            return next(
                holder for holder in left.logic.role_holders(agreement, role)
                if not holder["is_self"]
            )

        self.assertEqual(theirs()["status"], "requested")
        # Confirming is an ordinary offer. Their answer is already recorded,
        # so the holding goes live the moment both records exist - the
        # newcomer does not have to answer a second time.
        self.assertEqual(
            left.logic.offer_role(
                participant_uuid, right.session.identity.uuid,
            ).status,
            "ok",
        )
        sync(left, right)
        self.assertEqual(theirs()["status"], "accepted")

    def test_revoking_a_confirmed_request_does_not_read_as_a_fresh_request(self):
        # The reason a revoked offer is marked rather than deleted. Asking,
        # being confirmed, then being revoked leaves the asker's answer in
        # place; if the withdrawal left no trace, that answer would read as
        # somebody asking again and the Identity holder would be prompted to
        # re-offer exactly what they had just taken back.
        runtime = self.runtime(9511)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        role_uuid = runtime.logic.create_role(agreement_uuid, "Treasurer").value
        mine = runtime.session.identity.uuid

        def statuses():
            agreement = runtime.session.protocol.index[agreement_uuid]
            role = runtime.session.protocol.index[role_uuid]
            return [
                holder["status"]
                for holder in runtime.logic.role_holders(agreement, role)
            ]

        runtime.logic.decide_role(role_uuid, "accepted")
        self.assertEqual(statuses(), ["requested"])
        runtime.logic.offer_role(role_uuid, mine)
        self.assertEqual(statuses(), ["accepted"])
        runtime.logic.revoke_role_offer(role_uuid, mine)
        self.assertEqual(statuses(), ["revoked"])

        # Offering again revives the same record rather than laying a second
        # one beside it, and the answer already on file still counts.
        runtime.logic.offer_role(role_uuid, mine)
        self.assertEqual(statuses(), ["accepted"])
        role = runtime.session.protocol.index[role_uuid]
        self.assertEqual(len(runtime.logic._all_role_offers(role)), 1)

        # Once the person clears their own answer, nothing lingers on show.
        runtime.logic.revoke_role_offer(role_uuid, mine)
        runtime.logic.resign_role(role_uuid)
        self.assertEqual(statuses(), [])

    def test_an_offer_from_someone_who_is_not_identity_is_not_adopted(self):
        # The affordance keeps an honest client from making such an offer;
        # this is the other half, for a client that ignores it.
        left, right = self.runtime(9503), self.runtime(9504)
        agreement_uuid = left.logic.create_agreement("Charter").value
        role_uuid = left.logic.create_role(agreement_uuid, "Treasurer").value
        connect(left, right, agreement_uuid)
        right.logic.accept_agreement_invitation(
            right.session.protocol.index[agreement_uuid],
        )
        sync(left, right)

        forged = right.session.create_child(
            role_uuid,
            {
                "type": "agreement_role_offer",
                "actor_uuid": right.session.identity.uuid,
                "actor_kind": "individual",
                "offered_by": right.session.identity.uuid,
                "offered_at": "2026-01-01T00:00:00Z",
            },
            {},
        ).value
        sync(left, right)

        rejected = left.logic.accept_peer_node(right.peer_addr, forged.uuid)
        self.assertEqual(rejected.status, "error")
        self.assertIn("Identity holder", rejected.reason)

    def test_an_answer_from_a_peer_you_do_not_sync_with_is_unobserved(self):
        # "They have not answered" and "I cannot see whether they have" are
        # different facts, and a decision is credible only from the actor's
        # own replica. Reporting the second as pending would be a lie.
        runtime = self.runtime(9505)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        agreement = runtime.session.protocol.index[agreement_uuid]
        role_uuid = runtime.logic.create_role(agreement_uuid, "Treasurer").value

        runtime.logic.offer_role(role_uuid, "an-actor-we-never-meet")
        role = runtime.session.protocol.index[role_uuid]
        holders = runtime.logic.role_holders(agreement, role)

        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0]["status"], "unobserved")
        self.assertIsNone(holders[0]["decided_at"])

    def test_a_role_acceptance_covers_the_document_and_that_role_only(self):
        runtime = self.runtime(9506)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        treasurer_uuid = runtime.logic.create_role(
            agreement_uuid, "Treasurer",
        ).value
        secretary_uuid = runtime.logic.create_role(
            agreement_uuid, "Secretary",
        ).value
        mine = runtime.session.identity.uuid
        runtime.logic.offer_role(treasurer_uuid, mine)
        runtime.logic.decide_role(treasurer_uuid, "accepted")

        def status():
            agreement = runtime.session.protocol.index[agreement_uuid]
            role = runtime.session.protocol.index[treasurer_uuid]
            return runtime.logic.role_holders(agreement, role)[0]["status"]

        self.assertEqual(status(), "accepted")
        # Somebody else's role is not this participant's business.
        runtime.logic.create_role_item(
            secretary_uuid, "accountability", "Take minutes",
        )
        runtime.logic.rename_role(secretary_uuid, "Clerk")
        self.assertEqual(status(), "accepted")
        # Their own role is.
        runtime.logic.create_role_item(
            treasurer_uuid, "accountability", "Monthly reconciliation",
        )
        self.assertEqual(status(), "outdated")
        runtime.logic.decide_role(treasurer_uuid, "accepted")
        self.assertEqual(status(), "accepted")
        # So is the document everybody is agreeing to.
        runtime.logic.create_section(agreement_uuid, "Terms")
        self.assertEqual(status(), "outdated")

    def test_a_holder_is_only_accepted_once_seen_from_their_own_replica(self):
        left, right = self.runtime(9507), self.runtime(9508)
        agreement_uuid = left.logic.create_agreement("Charter").value
        agreement = left.session.protocol.index[agreement_uuid]
        role_uuid = left.logic.create_role(agreement_uuid, "Treasurer").value
        connect(left, right, agreement_uuid)
        right.logic.accept_agreement_invitation(
            right.session.protocol.index[agreement_uuid],
        )
        sync(left, right)
        left.logic.offer_role(role_uuid, right.session.identity.uuid)
        sync(left, right)

        def status_from_left():
            role = left.session.protocol.index[role_uuid]
            return next(
                holder["status"]
                for holder in left.logic.role_holders(agreement, role)
                if not holder["is_self"]
            )

        self.assertEqual(status_from_left(), "pending")
        right.logic.decide_role(role_uuid, "accepted")
        sync(left, right)
        self.assertEqual(status_from_left(), "accepted")
        right.logic.decide_role(role_uuid, "refused")
        sync(left, right)
        self.assertEqual(status_from_left(), "refused")

    def two_parents(self, runtime, ports):
        """An agreement seated in two others, Alpha first."""
        alpha = runtime.logic.create_agreement("Alpha").value
        beta = runtime.logic.create_agreement("Beta").value
        circle = runtime.logic.create_subagreement(alpha, "Circle").value
        seat = runtime.logic.create_role(beta, "Delegate").value
        self.assertEqual(
            runtime.logic.offer_role(seat, circle).status, "ok",
        )
        self.assertEqual(
            runtime.logic.seat_agreement(seat, circle).status, "ok",
        )
        return alpha, beta, circle

    def test_one_parent_going_invalid_does_not_close_a_second_one(self):
        # ANY path, not all: holding a seat in two agreements means either
        # can carry it, so one parent lapsing suspends that relationship
        # rather than paralysing a body the other still depends on.
        runtime = self.runtime(9514)
        alpha, beta, circle = self.two_parents(runtime, None)

        def writable():
            return runtime.logic.interaction_payload(
                runtime.session.protocol.index[circle],
            )["allowed"]

        self.assertTrue(writable())
        self.leave(runtime, alpha)
        self.assertTrue(writable())
        self.leave(runtime, beta)
        self.assertFalse(writable())
        # And it comes back through whichever parent recovers.
        self.rejoin(runtime, beta)
        self.assertTrue(writable())

    def test_home_is_the_first_seat_that_works_and_falls_back(self):
        runtime = self.runtime(9515)
        alpha, beta, circle = self.two_parents(runtime, None)

        def home():
            return runtime.logic.home_parent_uuid(
                runtime.session.protocol.index[circle],
            )

        self.assertEqual(home(), alpha)
        # Home is derived, so it moves on when the first seat stops working
        # and moves back when it recovers - there is no stored home to
        # disagree with the holdings.
        self.leave(runtime, alpha)
        self.assertEqual(home(), beta)
        self.rejoin(runtime, alpha)
        self.assertEqual(home(), alpha)
        # With no seat working there is no home, and it draws as a root.
        self.leave(runtime, alpha)
        self.leave(runtime, beta)
        self.assertEqual(home(), "")

    def test_reordering_the_seats_changes_where_it_is_drawn(self):
        runtime = self.runtime(9516)
        alpha, beta, circle = self.two_parents(runtime, None)
        holdings = runtime.logic.parent_holdings(
            runtime.session.protocol.index[circle],
        )
        self.assertEqual(len(holdings), 2)

        self.assertEqual(
            runtime.logic.move_parent_holding(holdings[1].uuid, 0).status, "ok",
        )
        self.assertEqual(
            runtime.logic.home_parent_uuid(
                runtime.session.protocol.index[circle],
            ),
            beta,
        )

    def test_the_organization_is_drawn_through_home_and_names_the_rest(self):
        runtime = self.runtime(9517)
        alpha, beta, circle = self.two_parents(runtime, None)

        organization = runtime.logic.organization_payload()
        roots = {item["uuid"]: item for item in organization["roots"]}
        # Drawn once, under home, with the other seat named rather than
        # hidden - a second parent nobody can see is a trap for whoever
        # deletes the first.
        self.assertEqual(set(roots), {alpha, beta})
        self.assertEqual(
            [child["uuid"] for child in roots[alpha]["children"]], [circle],
        )
        self.assertEqual(roots[beta]["children"], [])
        drawn = roots[alpha]["children"][0]
        self.assertEqual(drawn["home_parent_uuid"], alpha)
        self.assertEqual(
            [item["uuid"] for item in drawn["other_parents"]], [beta],
        )

    def test_a_seat_that_would_close_a_loop_is_refused(self):
        # Best-effort per replica, and best effort is still worth making.
        runtime = self.runtime(9518)
        alpha = runtime.logic.create_agreement("Alpha").value
        circle = runtime.logic.create_subagreement(alpha, "Circle").value
        inner = runtime.logic.create_subagreement(circle, "Inner").value

        seat = runtime.logic.create_role(inner, "Upward").value
        self.assertEqual(runtime.logic.offer_role(seat, alpha).status, "ok")
        looped = runtime.logic.seat_agreement(seat, alpha)

        self.assertEqual(looped.status, "error")
        self.assertIn("circular", looped.reason)

    def test_only_the_seated_agreements_identity_holder_may_take_a_seat(self):
        left, right = self.runtime(9519), self.runtime(9520)
        alpha = left.logic.create_agreement("Alpha").value
        circle = left.logic.create_subagreement(alpha, "Circle").value
        beta = right.logic.create_agreement("Beta").value
        seat = right.logic.create_role(beta, "Delegate").value

        # right holds Beta's Identity, but not Circle's, and cannot answer
        # for a body that is not theirs to speak for.
        refused = right.logic.seat_agreement(seat, circle)
        self.assertEqual(refused.status, "error")

    def test_somebody_known_but_not_on_this_topic_keeps_their_name(self):
        # "Not invited to this agreement yet" and "I cannot see their answer"
        # are different facts, and only the first one can be acted on. The
        # earlier wording reported both as unobserved and threw the name
        # away, which made a real person look like a stranger.
        left, right = self.runtime(9521), self.runtime(9522)
        shared = left.logic.create_agreement("Shared").value
        connect(left, right, shared)
        sync(left, right)

        # A second agreement right was never invited to.
        other = left.logic.create_agreement("Private").value
        role_uuid = left.logic.create_role(other, "Treasurer").value
        left.logic.offer_role(role_uuid, right.session.identity.uuid)

        agreement = left.session.protocol.index[other]
        role = left.session.protocol.index[role_uuid]
        holder = next(
            item for item in left.logic.role_holders(agreement, role)
            if not item["is_self"]
        )
        self.assertEqual(holder["status"], "uninvited")
        self.assertNotEqual(holder["name"], "Somebody you have not met")
        # And an actor nobody can place at all is still unobserved.
        left.logic.offer_role(role_uuid, "nobody-we-have-ever-met")
        role = left.session.protocol.index[role_uuid]
        stranger = next(
            item for item in left.logic.role_holders(agreement, role)
            if item["actor_uuid"] == "nobody-we-have-ever-met"
        )
        self.assertEqual(stranger["status"], "unobserved")

    def test_a_read_never_serves_what_an_edit_has_already_changed(self):
        # Building one payload asks Session for the same identity, members and
        # hashes hundreds of times, so reads memoise. The scope is the read:
        # nothing outside one caches, or an edit would be invisible until
        # something else happened to clear it.
        runtime = self.runtime(9523)
        agreement_uuid = runtime.logic.create_agreement("Charter").value

        def own_roles():
            payload = runtime.logic.document_payload(agreement_uuid)
            me = next(
                person for person in payload["participants"]
                if person["is_self"]
            )
            return {role["name"]: role["status"] for role in me["roles"]}

        self.assertEqual(own_roles()["Participant"], "accepted")
        runtime.logic.create_section(agreement_uuid, "Purpose")
        self.assertEqual(own_roles()["Participant"], "outdated")
        agreement = runtime.session.protocol.index[agreement_uuid]
        role = runtime.logic.roles(agreement)[0]
        runtime.logic.decide_role(role.uuid, "accepted")
        self.assertEqual(own_roles()["Participant"], "accepted")

    def test_the_creator_holds_identity_of_what_they_create(self):
        runtime = self.runtime(9488)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        child_uuid = runtime.logic.create_subagreement(
            agreement_uuid, "Operations",
        ).value

        for uuid in (agreement_uuid, child_uuid):
            agreement = runtime.session.protocol.index[uuid]
            self.assertTrue(runtime.logic.holds_identity(agreement))
            self.assertEqual(
                runtime.logic.identity_holder(agreement),
                runtime.session.identity.uuid,
            )
        payload = runtime.logic.document_payload(agreement_uuid)
        self.assertEqual(payload["identity"]["state"], "held")
        self.assertTrue(payload["identity"]["is_self"])
        self.assertEqual(payload["identity"]["claims"], [])

    def test_identity_is_a_record_beside_the_document_not_inside_it(self):
        runtime = self.runtime(9489)
        agreement_uuid = runtime.logic.create_agreement("Charter").value

        def acceptance():
            return self.own_standing(runtime, agreement_uuid)

        payload = runtime.logic.document_payload(agreement_uuid)
        # Not document content, so it never renders as a document change.
        self.assertNotIn(
            "agreement_identity",
            {child["data"].get("type")
             for child in payload["agreement"]["children"]},
        )
        # A handover changes who holds a role, not what the agreement says,
        # so it must not re-open everybody's acceptance.
        self.assertEqual(acceptance(), "accepted")
        other = "another-actor-uuid"
        self.assertEqual(
            runtime.logic.offer_identity(agreement_uuid, other).status, "ok",
        )
        self.assertEqual(acceptance(), "accepted")

    def test_only_the_holder_hands_identity_on_but_anyone_may_take_it(self):
        left, right = self.runtime(9490), self.runtime(9491)
        agreement_uuid = left.logic.create_agreement("Charter").value
        connect(left, right, agreement_uuid)
        right.logic.accept_agreement_invitation(
            right.session.protocol.index[agreement_uuid],
        )

        agreement = right.session.protocol.index[agreement_uuid]
        self.assertFalse(right.logic.holds_identity(agreement))
        handed = right.logic.offer_identity(
            agreement_uuid, right.session.identity.uuid,
        )
        self.assertEqual(handed.status, "error")
        self.assertIn("only the Identity holder", handed.reason)
        # Taking is never refused. No rule read from an observer-relative
        # view can tell a vacant seat from a holder you do not sync with, so
        # the seat is takeable and the conflict is surfaced instead.
        self.assertEqual(right.logic.take_identity(agreement_uuid).status, "ok")
        self.assertTrue(
            right.logic.holds_identity(
                right.session.protocol.index[agreement_uuid],
            ),
        )

    def test_identity_handover_converges_and_a_claim_diverges(self):
        left, right = self.runtime(9492), self.runtime(9493)
        agreement_uuid = left.logic.create_agreement("Charter").value
        connect(left, right, agreement_uuid)
        right.logic.accept_agreement_invitation(
            right.session.protocol.index[agreement_uuid],
        )
        sync(left, right)

        # Right takes the seat while left, holding it, does nothing. One side
        # wrote, so this arrives as an ordinary peer change rather than a
        # divergence - the seat is contested in meaning but not in the
        # protocol's sense, and the same adopt/rollback settles it either way.
        right.logic.take_identity(agreement_uuid)
        sync(left, right)
        contested = left.logic.document_payload(agreement_uuid)["identity"]
        node_uuid = contested["node_uuid"]
        self.assertEqual(
            left.logic.document_payload(
                agreement_uuid,
            )["transition_by_node"][node_uuid]["type"],
            "peer_made_changes",
        )
        self.assertEqual(len(contested["claims"]), 1)
        self.assertEqual(
            contested["claims"][0]["holder_actor_uuid"],
            right.session.identity.uuid,
        )
        # The peer naming itself is accepting a handover rather than staking
        # a claim over a third party - the view has to say which.
        self.assertTrue(contested["claims"][0]["is_handover"])

        # Adopting the peer's node is the whole resolution: no separate
        # accept step, because agreeing on the node *is* the consent.
        self.assertEqual(
            left.logic.accept_peer_node(right.peer_addr, node_uuid).status, "ok",
        )
        sync(left, right)
        settled = left.logic.document_payload(agreement_uuid)["identity"]
        self.assertEqual(settled["claims"], [])
        self.assertEqual(
            settled["holder_actor_uuid"], right.session.identity.uuid,
        )
        self.assertFalse(
            left.logic.holds_identity(
                left.session.protocol.index[agreement_uuid],
            ),
        )

    def test_two_sides_naming_different_holders_at_once_diverge(self):
        left, right = self.runtime(9495), self.runtime(9496)
        agreement_uuid = left.logic.create_agreement("Charter").value
        connect(left, right, agreement_uuid)
        right.logic.accept_agreement_invitation(
            right.session.protocol.index[agreement_uuid],
        )
        sync(left, right)

        # Both write the same node without seeing the other: left hands the
        # seat to a third party, right takes it. That is a real divergence,
        # and it is the same node, which is the whole reason the single-node
        # encoding works - two separate claim nodes could never diverge.
        left.logic.offer_identity(agreement_uuid, "third-actor-uuid")
        right.logic.take_identity(agreement_uuid)
        sync(left, right)

        payload = left.logic.document_payload(agreement_uuid)
        node_uuid = payload["identity"]["node_uuid"]
        self.assertEqual(
            payload["transition_by_node"][node_uuid]["type"], "divergence",
        )
        self.assertEqual(
            left.logic.accept_peer_node(right.peer_addr, node_uuid).status, "ok",
        )
        settled = left.logic.document_payload(agreement_uuid)
        self.assertNotEqual(
            settled["transition_by_node"].get(node_uuid, {}).get("type"),
            "divergence",
        )
        self.assertEqual(
            settled["identity"]["holder_actor_uuid"],
            right.session.identity.uuid,
        )

    def test_identity_writes_obey_the_read_only_guard(self):
        runtime = self.runtime(9494)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_subagreement(
            parent_uuid, "Operations",
        ).value
        self.leave(runtime, parent_uuid)

        for result in (
            runtime.logic.take_identity(child_uuid),
            runtime.logic.offer_identity(child_uuid, "someone"),
        ):
            self.assertEqual(result.status, "error")
            self.assertIn("Read-only", result.reason)

    def test_a_role_carries_accountabilities_and_domains_as_nodes(self):
        runtime = self.runtime(9480)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        role_uuid = runtime.logic.create_role(agreement_uuid, "Treasurer").value
        runtime.logic.set_role_purpose(
            role_uuid, "Keep the books honest and current",
        )
        accountability = runtime.logic.create_role_item(
            role_uuid, "accountability", "Monthly reconciliation",
        )
        domain = runtime.logic.create_role_item(
            role_uuid, "domain", "Bank accounts",
        )

        self.assertEqual(accountability.status, "ok")
        self.assertEqual(domain.status, "ok")
        agreement = runtime.session.protocol.index[agreement_uuid]
        # Every agreement starts with a Participant role, so this one is
        # the second.
        roles = runtime.logic.roles(agreement)
        self.assertEqual(
            [node.data["name"] for node in roles],
            ["Participant", "Treasurer"],
        )
        treasurer = roles[1]
        self.assertEqual(
            treasurer.data["purpose"], "Keep the books honest and current",
        )
        # Separate nodes, not a list inside the role: two people editing
        # different accountabilities have to be able to diverge separately.
        self.assertEqual(
            [node.data["text"] for node in
             runtime.logic.accountabilities(treasurer)],
            ["Monthly reconciliation"],
        )
        self.assertEqual(
            [node.data["text"] for node in runtime.logic.domains(treasurer)],
            ["Bank accounts"],
        )

    def test_roles_and_their_items_reorder_within_their_own_type(self):
        runtime = self.runtime(9481)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        first = runtime.logic.create_role(agreement_uuid, "First").value
        second = runtime.logic.create_role(agreement_uuid, "Second").value
        runtime.logic.create_role(agreement_uuid, "Third")
        alpha = runtime.logic.create_role_item(
            first, "accountability", "Alpha",
        ).value
        runtime.logic.create_role_item(first, "accountability", "Beta")
        # A domain shares the role with the accountabilities, so an ordering
        # that was not type-scoped would interleave them.
        gamma = runtime.logic.create_role_item(first, "domain", "Gamma").value
        runtime.logic.create_role_item(first, "domain", "Delta")

        def names():
            agreement = runtime.session.protocol.index[agreement_uuid]
            return [node.data["name"] for node in runtime.logic.roles(agreement)]

        def texts(reader):
            role = runtime.session.protocol.index[first]
            return [node.data["text"] for node in reader(role)]

        self.assertEqual(
            names(), ["Participant", "First", "Second", "Third"],
        )
        self.assertEqual(runtime.logic.move_role(second, 0).status, "ok")
        self.assertEqual(
            names(), ["Second", "Participant", "First", "Third"],
        )

        self.assertEqual(texts(runtime.logic.accountabilities), ["Alpha", "Beta"])
        self.assertEqual(runtime.logic.move_role_item(alpha, 1).status, "ok")
        self.assertEqual(texts(runtime.logic.accountabilities), ["Beta", "Alpha"])
        # Moving an accountability left the domains alone.
        self.assertEqual(texts(runtime.logic.domains), ["Gamma", "Delta"])
        self.assertEqual(runtime.logic.move_role_item(gamma, 1).status, "ok")
        self.assertEqual(texts(runtime.logic.domains), ["Delta", "Gamma"])

    def test_deleting_a_role_takes_its_accountabilities_and_domains(self):
        runtime = self.runtime(9482)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        role_uuid = runtime.logic.create_role(agreement_uuid, "Treasurer").value
        item_uuid = runtime.logic.create_role_item(
            role_uuid, "accountability", "Monthly reconciliation",
        ).value

        self.assertEqual(runtime.logic.delete_role(role_uuid).status, "ok")
        # Deleting a container prunes its descendants out of the index rather
        # than tombstoning each one, as it does for a section's clauses.
        self.assertNotIn(item_uuid, runtime.session.protocol.index)
        agreement = runtime.session.protocol.index[agreement_uuid]
        self.assertEqual(
            [node.data["name"] for node in runtime.logic.roles(agreement)],
            ["Participant"],
        )

    def test_a_purpose_may_be_cleared_but_a_name_may_not(self):
        runtime = self.runtime(9483)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        role_uuid = runtime.logic.create_role(agreement_uuid, "Treasurer").value
        runtime.logic.set_role_purpose(role_uuid, "Keep the books")

        self.assertEqual(
            runtime.logic.set_role_purpose(role_uuid, "").status, "ok",
        )
        role = runtime.session.protocol.index[role_uuid]
        self.assertEqual(role.data["purpose"], "")
        # The name is what identifies the role, so blanking it is refused.
        self.assertEqual(
            runtime.logic.rename_role(role_uuid, "   ").status, "error",
        )
        self.assertEqual(
            runtime.session.protocol.index[role_uuid].data["name"], "Treasurer",
        )

    def test_role_writes_reject_unknown_kinds_and_wrong_node_types(self):
        runtime = self.runtime(9484)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        role_uuid = runtime.logic.create_role(agreement_uuid, "Treasurer").value
        section_uuid = runtime.logic.create_section(agreement_uuid, "Terms").value

        self.assertEqual(
            runtime.logic.create_role(agreement_uuid, "  ").status, "error",
        )
        self.assertEqual(
            runtime.logic.create_role_item(role_uuid, "budget", "x").status,
            "error",
        )
        # A section is not a role, and a role is not a role item.
        self.assertEqual(
            runtime.logic.create_role_item(
                section_uuid, "accountability", "x",
            ).status,
            "error",
        )
        self.assertEqual(runtime.logic.delete_role(section_uuid).status, "error")
        self.assertEqual(
            runtime.logic.update_role_item(role_uuid, "x").status, "error",
        )

    def test_role_writes_obey_the_read_only_guard(self):
        runtime = self.runtime(9486)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_subagreement(
            parent_uuid, "Operations",
        ).value
        role_uuid = runtime.logic.create_role(child_uuid, "Treasurer").value
        item_uuid = runtime.logic.create_role_item(
            role_uuid, "accountability", "Monthly reconciliation",
        ).value
        self.leave(runtime, parent_uuid)

        for result in (
            runtime.logic.create_role(child_uuid, "Blocked"),
            runtime.logic.rename_role(role_uuid, "Changed"),
            runtime.logic.set_role_purpose(role_uuid, "Changed"),
            runtime.logic.move_role(role_uuid, 0),
            runtime.logic.delete_role(role_uuid),
            runtime.logic.create_role_item(role_uuid, "domain", "Blocked"),
            runtime.logic.update_role_item(item_uuid, "Changed"),
            runtime.logic.move_role_item(item_uuid, 0),
            runtime.logic.delete_role_item(item_uuid),
        ):
            self.assertEqual(result.status, "error")
            self.assertIn("Read-only", result.reason)

    def test_role_nodes_are_reactable_and_owned(self):
        runtime = self.runtime(9487)
        agreement_uuid = runtime.logic.create_agreement("Charter").value
        role_uuid = runtime.logic.create_role(agreement_uuid, "Treasurer").value
        item_uuid = runtime.logic.create_role_item(
            role_uuid, "domain", "Bank accounts",
        ).value

        # Reactable, or a divergence on a role would have no way out.
        for node_type in (
            "agreement_role", "agreement_accountability", "agreement_domain",
        ):
            self.assertIn(node_type, AgreementLogic.REACTABLE)
            self.assertIn(node_type, AgreementLogic.OWNED_NODE_TYPES)
        self.assertTrue(runtime.logic.owns_node(role_uuid))
        self.assertTrue(runtime.logic.owns_node(item_uuid))

    # A seat is offered on the parent's page and answered on the child's,
    # because those are two different people's pages.
    def test_a_seat_offered_to_an_agreement_is_listed_on_it(self):
        runtime = self.runtime(9500)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_agreement("Team A").value
        role_uuid = runtime.logic.create_role(parent_uuid, "Operations").value
        runtime.logic.set_role_purpose(role_uuid, "Run the day to day")
        runtime.logic.offer_role(role_uuid, child_uuid)

        child = runtime.session.protocol.index[child_uuid]
        offers = runtime.logic.seat_offers(child)

        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["role_uuid"], role_uuid)
        self.assertEqual(offers[0]["role_name"], "Operations")
        self.assertEqual(offers[0]["role_purpose"], "Run the day to day")
        self.assertEqual(offers[0]["agreement_uuid"], parent_uuid)
        self.assertEqual(offers[0]["title"], "Cooperative")
        self.assertEqual(offers[0]["answer"], "")
        self.assertFalse(offers[0]["circular"])
        # The parent's own page never lists it as an invitation to itself.
        parent = runtime.session.protocol.index[parent_uuid]
        self.assertEqual(runtime.logic.seat_offers(parent), [])

        # Accepting it fills the seat and takes it off the list.
        runtime.logic.seat_agreement(role_uuid, child_uuid)
        child = runtime.session.protocol.index[child_uuid]
        self.assertEqual(runtime.logic.seat_offers(child), [])
        self.assertEqual(
            [seat["role_uuid"] for seat in runtime.logic.parent_payload(child)],
            [role_uuid],
        )

    def test_declining_a_seat_answers_it_and_can_be_reconsidered(self):
        runtime = self.runtime(9501)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_agreement("Team A").value
        role_uuid = runtime.logic.create_role(parent_uuid, "Operations").value
        runtime.logic.offer_role(role_uuid, child_uuid)

        self.assertEqual(
            runtime.logic.decline_seat(role_uuid, child_uuid).status, "ok",
        )
        child = runtime.session.protocol.index[child_uuid]
        offers = runtime.logic.seat_offers(child)

        # Still listed, because turning it down is an answer that can change.
        self.assertEqual(offers[0]["answer"], "refused")
        self.assertEqual(runtime.logic.parent_holdings(child), [])
        role = runtime.session.protocol.index[role_uuid]
        self.assertFalse(runtime.logic._agreement_holds_role(role, child_uuid))

        # Changing its mind rewrites the one answer rather than adding a
        # second, or which of them counts would be down to iteration order.
        runtime.logic.seat_agreement(role_uuid, child_uuid)
        role = runtime.session.protocol.index[role_uuid]
        decisions = [
            node for node in role.live_children()
            if node.data.get("type") == "agreement_role_decision"
            and node.data.get("actor_uuid") == child_uuid
        ]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].data["decision"], "accepted")
        self.assertEqual(
            decisions[0].data["decided_by"], runtime.session.identity.uuid,
        )
        child = runtime.session.protocol.index[child_uuid]
        self.assertEqual(len(runtime.logic.parent_holdings(child)), 1)

    def test_stepping_out_of_a_seat_clears_both_sides_of_it(self):
        runtime = self.runtime(9525)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_subagreement(
            parent_uuid, "Finance circle",
        ).value
        parent = runtime.session.protocol.index[parent_uuid]
        role_uuid = runtime.logic.child_agreements(parent)[0][1].uuid

        stepped = runtime.logic.unseat_agreement(role_uuid, child_uuid)

        # Reported as done, rather than judged by whether there was a peer to
        # tell: an agreement nobody else has yet produces no effects at all.
        self.assertEqual(stepped.status, "ok")
        child = runtime.session.protocol.index[child_uuid]
        role = runtime.session.protocol.index[role_uuid]
        self.assertEqual(runtime.logic.parent_holdings(child), [])
        # And the parent no longer counts it as seated, so the two sides say
        # the same thing. The offer stands, so it can be answered again.
        self.assertFalse(runtime.logic._agreement_holds_role(role, child_uuid))
        offers = runtime.logic.seat_offers(child)
        self.assertEqual(
            [(item["role_uuid"], item["answer"]) for item in offers],
            [(role_uuid, "")],
        )
        self.assertEqual(
            runtime.logic.seat_agreement(role_uuid, child_uuid).status, "ok",
        )
        self.assertEqual(
            runtime.logic.home_parent_uuid(
                runtime.session.protocol.index[child_uuid],
            ),
            parent_uuid,
        )
        # Twice over is not an error the second time round, it is a fact:
        # there is no seat here to give up.
        runtime.logic.unseat_agreement(role_uuid, child_uuid)
        repeated = runtime.logic.unseat_agreement(role_uuid, child_uuid)
        self.assertEqual(repeated.status, "error")
        self.assertIn("does not hold that role", repeated.reason)

    def test_only_the_agreements_identity_holder_answers_for_it(self):
        host = self.runtime(9502)
        guest = self.runtime(9503)
        parent_uuid = host.logic.create_agreement("Cooperative").value
        child_uuid = host.logic.create_agreement("Team A").value
        role_uuid = host.logic.create_role(parent_uuid, "Operations").value
        host.logic.offer_role(role_uuid, child_uuid)
        # Hand the child on, so this session speaks for it no longer.
        host.logic.offer_identity(child_uuid, guest.session.identity.uuid)

        for result in (
            host.logic.seat_agreement(role_uuid, child_uuid),
            host.logic.decline_seat(role_uuid, child_uuid),
        ):
            self.assertEqual(result.status, "error")
            self.assertIn("Identity holder", result.reason)

    def test_a_seat_that_would_close_a_loop_is_shown_and_refused(self):
        runtime = self.runtime(9504)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_subagreement(
            parent_uuid, "Team A",
        ).value
        # Now offer the parent a seat in its own child.
        back_uuid = runtime.logic.create_role(child_uuid, "Sponsor").value
        runtime.logic.offer_role(back_uuid, parent_uuid)

        parent = runtime.session.protocol.index[parent_uuid]
        offers = runtime.logic.seat_offers(parent)

        # Shown, rather than hidden as if it had never come.
        self.assertEqual(len(offers), 1)
        self.assertTrue(offers[0]["circular"])
        taken = runtime.logic.seat_agreement(back_uuid, parent_uuid)
        self.assertEqual(taken.status, "error")
        self.assertIn("circular", taken.reason)
        # Turning it down does not walk the graph, so it still works.
        self.assertEqual(
            runtime.logic.decline_seat(back_uuid, parent_uuid).status, "ok",
        )

    def test_an_agreements_own_seat_reads_as_accepted_not_unobserved(self):
        runtime = self.runtime(9507)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_agreement("Team A").value
        role_uuid = runtime.logic.create_role(parent_uuid, "Operations").value
        runtime.logic.offer_role(role_uuid, child_uuid)

        parent = runtime.session.protocol.index[parent_uuid]
        role = runtime.session.protocol.index[role_uuid]
        # Offered and not yet answered, by somebody who could answer it.
        seat = next(
            holder for holder in runtime.logic.role_holders(parent, role)
            if holder["actor_uuid"] == child_uuid
        )
        self.assertEqual(seat["status"], "pending")
        self.assertEqual(seat["actor_kind"], "agreement")
        self.assertEqual(seat["name"], "Team A")

        runtime.logic.seat_agreement(role_uuid, child_uuid)
        parent = runtime.session.protocol.index[parent_uuid]
        role = runtime.session.protocol.index[role_uuid]
        seat = next(
            holder for holder in runtime.logic.role_holders(parent, role)
            if holder["actor_uuid"] == child_uuid
        )

        # An agreement has no replica of its own, so its answer is vouched
        # for by the replica of whoever gave it - this one. Reporting it as
        # unobserved would call this session unable to see what it wrote.
        self.assertEqual(seat["status"], "accepted")
        self.assertTrue(seat["joined"])
        # And it is a second actor here, which is what makes this working.
        self.assertIn(child_uuid, runtime.logic.actor_uuids(parent))
        self.assertEqual(runtime.logic.agreement_state(parent), "working")

    def test_a_seat_answered_by_somebody_else_stays_unobserved(self):
        host = self.runtime(9508)
        guest = self.runtime(9509)
        parent_uuid = host.logic.create_agreement("Cooperative").value
        child_uuid = host.logic.create_agreement("Team A").value
        role_uuid = host.logic.create_role(parent_uuid, "Operations").value
        host.logic.offer_role(role_uuid, child_uuid)
        # Somebody else speaks for the child now, and this session does not
        # sync with them, so its answer is out of reach rather than absent.
        host.logic.offer_identity(child_uuid, guest.session.identity.uuid)

        parent = host.session.protocol.index[parent_uuid]
        role = host.session.protocol.index[role_uuid]
        seat = next(
            holder for holder in host.logic.role_holders(parent, role)
            if holder["actor_uuid"] == child_uuid
        )

        self.assertEqual(seat["status"], "unobserved")
        self.assertNotIn(child_uuid, host.logic.actor_uuids(parent))

    def test_seat_offers_reach_the_payload(self):
        runtime = self.runtime(9505)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_agreement("Team A").value
        role_uuid = runtime.logic.create_role(parent_uuid, "Operations").value
        runtime.logic.offer_role(role_uuid, child_uuid)

        payload = runtime.logic.document_payload(child_uuid)

        self.assertEqual(
            [offer["role_uuid"] for offer in payload["seat_offers"]],
            [role_uuid],
        )
        self.assertEqual(
            runtime.logic.document_payload(parent_uuid)["seat_offers"], [],
        )

    def test_a_revoked_seat_offer_stops_being_an_invitation(self):
        runtime = self.runtime(9506)
        parent_uuid = runtime.logic.create_agreement("Cooperative").value
        child_uuid = runtime.logic.create_agreement("Team A").value
        role_uuid = runtime.logic.create_role(parent_uuid, "Operations").value
        runtime.logic.offer_role(role_uuid, child_uuid)
        runtime.logic.revoke_role_offer(role_uuid, child_uuid)

        child = runtime.session.protocol.index[child_uuid]

        self.assertEqual(runtime.logic.seat_offers(child), [])
        taken = runtime.logic.seat_agreement(role_uuid, child_uuid)
        self.assertEqual(taken.status, "error")

    # Templates (2.8). A count of actors, so there is no flag to assert on -
    # only who is in it.
    def test_copy_carries_the_text_and_none_of_the_taking_part(self):
        runtime = self.runtime(9490)
        source_uuid = runtime.logic.create_agreement("Cooperative").value
        section_uuid = runtime.logic.create_section(source_uuid, "Terms").value
        runtime.logic.create_clause(section_uuid, "Members meet monthly.")
        role_uuid = runtime.logic.create_role(source_uuid, "Treasurer").value
        runtime.logic.create_role_item(role_uuid, "accountability", "Books")
        runtime.logic.create_role_item(role_uuid, "domain", "Bank accounts")

        copy_uuid = runtime.logic.clone_agreement(source_uuid).value
        copy = runtime.session.protocol.index[copy_uuid]

        self.assertEqual(copy.data["title"], "Cooperative (template)")
        self.assertEqual(
            [node.data["title"] for node in runtime.logic.sections(copy)],
            ["Terms"],
        )
        copied_section = runtime.logic.sections(copy)[0]
        self.assertEqual(
            [node.data["text"] for node in runtime.logic.clauses(copied_section)],
            ["Members meet monthly."],
        )
        # The default Participant travels as content, because it is content.
        copied_roles = runtime.logic.roles(copy)
        self.assertEqual(
            [node.data["name"] for node in copied_roles],
            ["Participant", "Treasurer"],
        )
        treasurer = copied_roles[1]
        self.assertEqual(
            [node.data["text"] for node in runtime.logic.accountabilities(treasurer)],
            ["Books"],
        )
        self.assertEqual(
            [node.data["text"] for node in runtime.logic.domains(treasurer)],
            ["Bank accounts"],
        )
        # Fresh uuids, or an answer given in the original would count here:
        # acceptance is looked up by role uuid.
        self.assertNotEqual(treasurer.uuid, role_uuid)
        self.assertNotIn(copied_section.uuid, {section_uuid})
        # Nobody is in it: no Identity, no offers, no answers, no seats.
        self.assertEqual(runtime.logic.identity_holder(copy), "")
        self.assertEqual(runtime.logic.parent_holdings(copy), [])
        for role in copied_roles:
            self.assertEqual(runtime.logic.role_holders(copy, role), [])
        self.assertEqual(runtime.logic.actor_uuids(copy), set())

    def test_state_counts_actors_from_template_to_working(self):
        host = self.runtime(9491)
        guest = self.runtime(9492)
        source_uuid = host.logic.create_agreement("Cooperative").value
        copy_uuid = host.logic.clone_agreement(source_uuid).value
        copy = host.session.protocol.index[copy_uuid]

        # Nobody in it at all.
        self.assertEqual(host.logic.agreement_state(copy), "template")
        # Identity is a role, so holding it alone makes one actor.
        host.logic.take_identity(copy_uuid)
        copy = host.session.protocol.index[copy_uuid]
        self.assertEqual(host.logic.agreement_state(copy), "instantiated")
        # Accepting a role you already hold Identity in adds no second actor.
        role = host.logic.roles(copy)[0]
        host.logic.decide_role(role.uuid, "accepted")
        copy = host.session.protocol.index[copy_uuid]
        self.assertEqual(host.logic.agreement_state(copy), "instantiated")

        connect(host, guest, copy_uuid)
        guest_copy = guest.session.protocol.index[copy_uuid]
        guest.logic.accept_agreement_invitation(guest_copy)
        host.logic.offer_role(role.uuid, guest.session.identity.uuid)
        sync(host, guest)
        guest.logic.decide_role(role.uuid, "accepted")
        sync(host, guest)

        copy = host.session.protocol.index[copy_uuid]
        self.assertEqual(host.logic.agreement_state(copy), "working")

    def test_asking_for_a_role_does_not_make_you_an_actor(self):
        host = self.runtime(9493)
        guest = self.runtime(9494)
        agreement_uuid = host.logic.create_agreement("Cooperative").value
        connect(host, guest, agreement_uuid)
        guest.logic.accept_agreement_invitation(
            guest.session.protocol.index[agreement_uuid],
        )
        role = host.logic.roles(
            host.session.protocol.index[agreement_uuid],
        )[0]

        # An answer with no offer behind it is a request, not a holding.
        guest.logic.decide_role(role.uuid, "accepted")
        sync(host, guest)
        agreement = host.session.protocol.index[agreement_uuid]
        self.assertEqual(
            {
                holder["status"] for holder in
                host.logic.role_holders(agreement, role)
                if not holder["is_self"]
            },
            {"requested"},
        )
        self.assertEqual(host.logic.agreement_state(agreement), "instantiated")

    def test_a_template_can_be_written_but_not_offered(self):
        runtime = self.runtime(9495)
        source_uuid = runtime.logic.create_agreement("Cooperative").value
        copy_uuid = runtime.logic.clone_agreement(source_uuid).value
        role = runtime.logic.roles(
            runtime.session.protocol.index[copy_uuid],
        )[0]

        # A template is for editing, so its text is not read-only.
        self.assertEqual(
            runtime.logic.create_section(copy_uuid, "Terms").status, "ok",
        )
        self.assertEqual(
            runtime.logic.create_role(copy_uuid, "Treasurer").status, "ok",
        )
        # But nobody speaks for it, so it cannot seat anyone.
        offered = runtime.logic.offer_role(
            role.uuid, runtime.session.identity.uuid,
        )
        self.assertEqual(offered.status, "error")
        self.assertIn("Identity", offered.reason)

    def test_state_reaches_both_payloads(self):
        runtime = self.runtime(9496)
        source_uuid = runtime.logic.create_agreement("Cooperative").value
        copy_uuid = runtime.logic.clone_agreement(source_uuid).value

        payload = runtime.logic.document_payload(copy_uuid)
        states = {
            item["uuid"]: item.get("state")
            for item in payload["organization"]["roots"]
        }

        self.assertEqual(payload["state"], "template")
        self.assertEqual(states[copy_uuid], "template")
        self.assertEqual(states[source_uuid], "instantiated")

    def test_copying_needs_no_standing_in_the_original(self):
        runtime = self.runtime(9497)
        source_uuid = runtime.logic.create_agreement("Cooperative").value
        runtime.logic.create_section(source_uuid, "Terms")
        self.leave(runtime, source_uuid)

        copied = runtime.logic.clone_agreement(source_uuid, "Reused")

        self.assertEqual(copied.status, "ok")
        copy = runtime.session.protocol.index[copied.value]
        self.assertEqual(copy.data["title"], "Reused")
        self.assertEqual(
            [node.data["title"] for node in runtime.logic.sections(copy)],
            ["Terms"],
        )
        self.assertEqual(runtime.logic.clone_agreement("missing").status, "error")

    # Being part of an agreement is holding a role in it, so these stand
    # where an agreement-level accept or refuse used to.
    def leave(self, runtime, agreement_uuid):
        agreement = runtime.session.protocol.index[agreement_uuid]
        for role in runtime.logic.roles(agreement):
            runtime.logic.decide_role(role.uuid, "refused")
        # Identity is a role too, so it goes as well - otherwise the
        # person who speaks for the agreement never stops being in it.
        if runtime.logic.holds_identity(
            runtime.session.protocol.index[agreement_uuid],
        ):
            runtime.logic.offer_identity(agreement_uuid, "somebody-else")

    def rejoin(self, runtime, agreement_uuid, expires_at=None):
        runtime.logic.take_identity(agreement_uuid)
        agreement = runtime.session.protocol.index[agreement_uuid]
        for role in runtime.logic.roles(agreement):
            runtime.logic.decide_role(role.uuid, "accepted", expires_at)

    def own_standing(self, runtime, agreement_uuid):
        agreement = runtime.session.protocol.index[agreement_uuid]
        role = runtime.logic.roles(agreement)[0]
        return next(
            holder["status"]
            for holder in runtime.logic.role_holders(agreement, role)
            if holder["is_self"]
        )

    @staticmethod
    def relay_config(relay_root: str, identity: str, state_dir: str) -> dict:
        return {
            "relay_root": relay_root,
            "relay_identity": identity,
            "relay_state_file": str(Path(state_dir) / f"state-{identity}.json"),
        }

    def relay_root(self) -> str:
        """One folder for every client in a test. A client never polls a
        topic it was not given, so sharing the folder shares nothing."""
        if not getattr(self, "_relay_root", None):
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            self._relay_root = directory.name
        return self._relay_root

    def runtime(self, port: int):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = app_server.load_config(None, "agreement", {
            "agreement": {
                "app_module": "s_agreement.application",
                "application_id": "agreement",
                "asset_package": "s_agreement.assets",
                "ui_file": "agreement.html",
                "css_file": "agreement.css",
            },
        })
        config["storage_file"] = str(Path(directory.name) / f"{port}.json")
        config["relay_state_directory"] = str(Path(directory.name) / "relay")
        runtime = app_server.create_runtime(port, config)
        runtime._test_tmp = directory
        created = runtime.relay_manager.create_target({
            "name": f"relay {port}", "backend": "local", "root": self.relay_root(),
        })
        if created.status != "ok":
            raise RuntimeError(created.reason)
        runtime.relay_target = created.value
        runtime.relay = runtime.relay_manager.connection_for_target(created.value)
        # How the other client's registries name this one: a relay peer is a
        # publication identity, not an address anybody can reach.
        runtime.peer_addr = f"relay:{runtime.relay.identity}"
        return runtime


if __name__ == "__main__":
    unittest.main()
