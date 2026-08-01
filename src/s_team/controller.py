"""Starlette controller for the minimal S-Team view."""

from __future__ import annotations

from sovereign import application_json_response
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def build_routes(logic, runtime) -> list[Route]:
    async def api_document(request: Request):
        requested = request.query_params.get("agreement_uuid")
        return runtime.composite_response(
            lambda: logic.document_snapshot(requested),
            lambda snapshot: runtime.collaboration.network_info(
                snapshot.get("topic_uuid"),
            ),
            logic.merge_document_observation,
        )

    async def api_create_agreement(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.create_agreement(data.get("title", "")))

    async def api_create_subagreement(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.create_subagreement(
            data["parent_agreement_uuid"], data.get("title", ""),
        ))

    async def api_clone_agreement(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.clone_agreement(
            data["agreement_uuid"], data.get("title"),
        ))

    async def api_select_agreement(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.select_agreement(data["agreement_uuid"]))

    async def api_delete_agreement(request: Request):
        data = await request.json()
        return await _json_result(
            runtime, logic.delete_agreement(data["agreement_uuid"]),
        )

    async def api_create_section(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.create_section(
            data["agreement_uuid"], data.get("title", ""),
        ))

    async def api_create_clause(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.create_clause(
            data["section_uuid"], data.get("text", ""),
        ))

    async def api_update_clause(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.update_clause(
            data["clause_uuid"], data.get("text", ""),
        ))

    async def api_rename_agreement(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.rename_agreement(
            data["agreement_uuid"], data.get("title", ""),
        ))

    async def api_rename_section(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.rename_section(
            data["section_uuid"], data.get("title", ""),
        ))

    async def api_delete_section(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.delete_section(data["section_uuid"]))

    async def api_move_section(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.move_section(
            data["section_uuid"], int(data.get("index", 0)),
        ))

    async def api_move_clause(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.move_clause(
            data["clause_uuid"], int(data.get("index", 0)),
        ))

    async def api_delete_clause(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.delete_clause(data["clause_uuid"]))

    async def api_take_identity(request: Request):
        data = await request.json()
        return await _json_result(
            runtime, logic.take_identity(data["agreement_uuid"]),
        )

    async def api_offer_identity(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.offer_identity(
            data["agreement_uuid"], data.get("actor_uuid", ""),
        ))

    async def api_create_role(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.create_role(
            data["agreement_uuid"], data.get("name", ""),
        ))

    async def api_rename_role(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.rename_role(
            data["role_uuid"], data.get("name", ""),
        ))

    async def api_role_purpose(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.set_role_purpose(
            data["role_uuid"], data.get("purpose", ""),
        ))

    async def api_delete_role(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.delete_role(data["role_uuid"]))

    async def api_move_role(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.move_role(
            data["role_uuid"], int(data.get("index", 0)),
        ))

    async def api_offer_role(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.offer_role(
            data["role_uuid"], data.get("actor_uuid", ""),
        ))

    async def api_revoke_role(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.revoke_role_offer(
            data["role_uuid"], data.get("actor_uuid", ""),
        ))

    async def api_decide_role(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.decide_role(
            data["role_uuid"],
            data.get("decision", ""),
            data.get("expires_at"),
        ))

    async def api_resign_role(request: Request):
        data = await request.json()
        return await _json_result(
            runtime, logic.resign_role(data["role_uuid"]),
        )

    async def api_seat_agreement(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.seat_agreement(
            data["role_uuid"], data.get("agreement_uuid", ""),
        ))

    async def api_decline_seat(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.decline_seat(
            data["role_uuid"], data.get("agreement_uuid", ""),
        ))

    async def api_unseat_agreement(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.unseat_agreement(
            data["role_uuid"], data.get("agreement_uuid", ""),
        ))

    async def api_create_seated_agreement(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.create_seated_agreement(
            data["role_uuid"], data.get("title", ""),
        ))

    async def api_move_parent(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.move_parent_holding(
            data["holding_uuid"], int(data.get("index", 0)),
        ))

    async def api_create_role_item(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.create_role_item(
            data["role_uuid"], data.get("kind", ""), data.get("text", ""),
        ))

    async def api_update_role_item(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.update_role_item(
            data["item_uuid"], data.get("text", ""),
        ))

    async def api_delete_role_item(request: Request):
        data = await request.json()
        return await _json_result(
            runtime, logic.delete_role_item(data["item_uuid"]),
        )

    async def api_move_role_item(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.move_role_item(
            data["item_uuid"], int(data.get("index", 0)),
        ))

    async def api_agenda_create(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.create_agenda_item(
            data["agreement_uuid"], data.get("text", ""), data.get("priority"),
        ))

    async def api_agenda_delete(request: Request):
        data = await request.json()
        return await _json_result(
            runtime, logic.delete_agenda_item(data["item_uuid"]),
        )

    async def api_agenda_priority(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.set_agenda_item_priority(
            data["item_uuid"], data.get("priority"),
        ))

    async def api_agenda_move(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.move_agenda_item(
            data["item_uuid"], int(data.get("index", 0)),
        ))

    async def api_react(request: Request):
        data = await request.json()
        reaction = data.get("reaction", "adopt")
        node_uuid = data["node_uuid"]
        source_addr = data["source_addr"]
        absent = bool(data.get("absent"))
        if reaction == "rollback":
            result = logic.rollback_peer_node(source_addr, node_uuid, absent)
        else:
            result = logic.accept_peer_node(source_addr, node_uuid, absent)
        return await _json_result(runtime, result)

    async def api_adopt(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.adopt_peer_changes(
            data["source_addr"], data["agreement_uuid"],
        ))

    return [
        Route("/api/team/document", api_document),
        Route("/api/team/agreements/create", api_create_agreement, methods=["POST"]),
        Route(
            "/api/team/agreements/create_subagreement",
            api_create_subagreement,
            methods=["POST"],
        ),
        Route(
            "/api/team/agreements/clone",
            api_clone_agreement,
            methods=["POST"],
        ),
        Route("/api/team/agreements/select", api_select_agreement, methods=["POST"]),
        Route("/api/team/agreements/rename", api_rename_agreement, methods=["POST"]),
        Route("/api/team/agreements/delete", api_delete_agreement, methods=["POST"]),
        Route("/api/team/sections/create", api_create_section, methods=["POST"]),
        Route("/api/team/sections/rename", api_rename_section, methods=["POST"]),
        Route("/api/team/sections/delete", api_delete_section, methods=["POST"]),
        Route("/api/team/sections/move", api_move_section, methods=["POST"]),
        Route("/api/team/clauses/move", api_move_clause, methods=["POST"]),
        Route("/api/team/clauses/create", api_create_clause, methods=["POST"]),
        Route("/api/team/clauses/update", api_update_clause, methods=["POST"]),
        Route("/api/team/clauses/delete", api_delete_clause, methods=["POST"]),
        Route(
            "/api/team/identity/take", api_take_identity, methods=["POST"],
        ),
        Route(
            "/api/team/identity/offer",
            api_offer_identity,
            methods=["POST"],
        ),
        Route("/api/team/roles/create", api_create_role, methods=["POST"]),
        Route("/api/team/roles/rename", api_rename_role, methods=["POST"]),
        Route(
            "/api/team/roles/set_purpose",
            api_role_purpose,
            methods=["POST"],
        ),
        Route("/api/team/roles/delete", api_delete_role, methods=["POST"]),
        Route("/api/team/roles/move", api_move_role, methods=["POST"]),
        Route("/api/team/roles/offer", api_offer_role, methods=["POST"]),
        Route("/api/team/roles/seat", api_seat_agreement, methods=["POST"]),
        Route(
            "/api/team/roles/decline_seat",
            api_decline_seat,
            methods=["POST"],
        ),
        Route(
            "/api/team/roles/unseat",
            api_unseat_agreement,
            methods=["POST"],
        ),
        Route(
            "/api/team/roles/seat_new",
            api_create_seated_agreement,
            methods=["POST"],
        ),
        Route("/api/team/parents/move", api_move_parent, methods=["POST"]),
        Route("/api/team/roles/revoke", api_revoke_role, methods=["POST"]),
        Route("/api/team/roles/decide", api_decide_role, methods=["POST"]),
        Route("/api/team/roles/resign", api_resign_role, methods=["POST"]),
        Route(
            "/api/team/roles/items/create",
            api_create_role_item,
            methods=["POST"],
        ),
        Route(
            "/api/team/roles/items/update",
            api_update_role_item,
            methods=["POST"],
        ),
        Route(
            "/api/team/roles/items/delete",
            api_delete_role_item,
            methods=["POST"],
        ),
        Route(
            "/api/team/roles/items/move",
            api_move_role_item,
            methods=["POST"],
        ),
        Route("/api/team/agenda/create", api_agenda_create, methods=["POST"]),
        Route("/api/team/agenda/delete", api_agenda_delete, methods=["POST"]),
        Route("/api/team/agenda/set_priority", api_agenda_priority, methods=["POST"]),
        Route("/api/team/agenda/move", api_agenda_move, methods=["POST"]),
        Route("/api/team/react", api_react, methods=["POST"]),
        Route("/api/team/adopt", api_adopt, methods=["POST"]),
    ]


async def _json_result(runtime, result) -> JSONResponse:
    return await application_json_response(runtime, result)
