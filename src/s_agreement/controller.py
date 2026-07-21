"""Starlette controller for the minimal S-Agreement view."""

from __future__ import annotations

import asyncio

from sovereign import application_result_view, json_value
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def build_routes(logic, runtime, config: dict) -> list[Route]:
    async def api_document(request: Request):
        return JSONResponse(json_value(logic.document_payload(
            request.query_params.get("agreement_uuid"),
        )))

    async def api_create_agreement(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.create_agreement(data.get("title", "")))

    async def api_select_agreement(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.select_agreement(data["agreement_uuid"]))

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

    async def api_delete_clause(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.delete_clause(data["clause_uuid"]))

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
        Route("/api/agreement/document", api_document),
        Route("/api/agreement/agreements/create", api_create_agreement, methods=["POST"]),
        Route("/api/agreement/agreements/select", api_select_agreement, methods=["POST"]),
        Route("/api/agreement/agreements/rename", api_rename_agreement, methods=["POST"]),
        Route("/api/agreement/sections/create", api_create_section, methods=["POST"]),
        Route("/api/agreement/sections/rename", api_rename_section, methods=["POST"]),
        Route("/api/agreement/sections/delete", api_delete_section, methods=["POST"]),
        Route("/api/agreement/clauses/create", api_create_clause, methods=["POST"]),
        Route("/api/agreement/clauses/update", api_update_clause, methods=["POST"]),
        Route("/api/agreement/clauses/delete", api_delete_clause, methods=["POST"]),
        Route("/api/agreement/react", api_react, methods=["POST"]),
        Route("/api/agreement/adopt", api_adopt, methods=["POST"]),
    ]


async def _json_result(runtime, result) -> JSONResponse:
    deliveries = []
    if result.status == "ok":
        deliveries = await asyncio.to_thread(
            runtime.channel_manager.execute_effects, result.effects,
        )
        runtime.notify_change()
    view = application_result_view(result, deliveries)
    return JSONResponse(view.payload, status_code=200 if view.ok else 409)
