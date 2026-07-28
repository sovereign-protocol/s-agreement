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
        self.assertEqual(payload["agreement"]["children"][0]["uuid"], section_uuid)
        self.assertEqual(
            payload["agreement"]["children"][0]["children"][0]["uuid"],
            clause_uuid,
        )

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
        section = payload["agreement"]["children"][0]
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
            live = [s for s in payload["agreement"]["children"] if not s["deleted"]]
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
        live = [item for item in sections if not item["deleted"]]
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
        clauses = payload["agreement"]["children"][0]["children"]
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
