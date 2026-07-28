import asyncio
import json
import unittest

from s_agreement.controller import build_routes
from s_agreement.logic import AgreementLogic
from sovereign import Session
from starlette.requests import Request


class _Runtime:
    def deliver_effects(self, effects):
        return []

    def notify_change(self):
        pass


def _post_request(path: str, payload: dict) -> Request:
    body = json.dumps(payload).encode()
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }, receive)


class AgreementOwnershipControllerTests(unittest.TestCase):
    def setUp(self):
        self.session = Session("local")
        self.logic = AgreementLogic(self.session)
        self.logic.create_agreement("Local agreement")
        self.routes = build_routes(self.logic, _Runtime())

    def _post(self, path: str, payload: dict):
        endpoint = next(route.endpoint for route in self.routes if route.path == path)
        return asyncio.run(endpoint(_post_request(path, payload)))

    def test_clause_mutation_rejects_an_agreement_typed_node_outside_an_agreement(self):
        foreign = self.session.create_child(
            self.session.root_uuid(),
            {"type": "agreement_clause", "text": "foreign"},
            {},
        ).value

        response = self._post("/api/agreement/clauses/update", {
            "clause_uuid": foreign.uuid,
            "text": "overwritten",
        })

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.session.protocol.index[foreign.uuid].data["text"], "foreign",
        )

    def test_react_rejects_a_peer_only_agreement_node_under_a_foreign_topic(self):
        peer = Session("peer")
        foreign_topic = peer.create_child(
            peer.root_uuid(), {"type": "kanban_board", "name": "foreign"}, {},
        ).value
        self.session.adopt_subtree(
            peer.get_node(foreign_topic.uuid), self.session.root_uuid(),
        )
        peer_clause = peer.create_child(
            foreign_topic.uuid,
            {"type": "agreement_clause", "text": "foreign"},
            {},
        ).value
        self.session.apply_peer_subtree(
            "peer", peer.get_node(foreign_topic.uuid), None,
        )
        self.session.note_indirect_peer_topic("peer", foreign_topic.uuid)

        response = self._post("/api/agreement/react", {
            "source_addr": "peer",
            "node_uuid": peer_clause.uuid,
            "reaction": "adopt",
        })

        self.assertEqual(response.status_code, 409)
        self.assertNotIn(peer_clause.uuid, self.session.protocol.index)


if __name__ == "__main__":
    unittest.main()
