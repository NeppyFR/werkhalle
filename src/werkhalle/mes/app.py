"""HTTP and WebSocket surface of the MES (docs/API.md). Every route is a thin wrapper around `Runtime`.

Trace queries read SQLite, so they are plain `def` routes and run on the server's thread pool. Everything else is `async def` and
runs on the one event loop the runtime owns.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .opcua_client import PlantNotConnected
from .runtime import ApiError, Runtime, WS_SEND_TIMEOUT_S, WsClient, to_json

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
MAX_BODY_BYTES = 1_048_576
MAX_LIMIT = 5000
_READ_METHODS = ("GET", "HEAD")

# The Windows registry maps .js to text/plain on many machines, and browsers refuse to load such an ES module.
for _extension, _type in ((".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css"),
                          (".svg", "image/svg+xml"), (".json", "application/json")):
    mimetypes.add_type(_type, _extension)


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message, **extra})


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} ist in JSON nicht erlaubt")


async def _body(request: Request) -> dict:
    # Without a Content-Length a chunked body has to be counted on the way in.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise ApiError(413, "too_large", "Die Anfrage ist zu groß.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return {}
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise ApiError(400, "bad_request", "Der Inhalt der Anfrage ist kein gültiges JSON.") from exc
    if not isinstance(value, dict):
        raise ApiError(400, "bad_request", "Die Anfrage muss ein JSON-Objekt sein.")
    return value


def _json(data) -> Response:
    """Strict JSON without the walk over every value that FastAPI's encoder does on the loop, which took half a second on a big twin run."""
    try:
        text = to_json(data)
    except TypeError:
        text = to_json(jsonable_encoder(data))
    return Response(content=text.encode("utf-8"), media_type="application/json")


def _limit(value: int) -> int:
    return max(0, min(value, MAX_LIMIT))


class LocalOnly:
    """Both servers listen on 127.0.0.1 only, but a page from elsewhere can still reach them through the browser.

    So: the Host must name this server (DNS rebinding), a present Origin must too on anything that changes state or opens a socket,
    and a body must declare itself as JSON, which rules out the forms and text/plain requests a browser sends without a preflight.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        refused = self._refusal(scope)
        if refused is None:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008})
        else:
            await _error(*refused)(scope, receive, send)

    @staticmethod
    def _refusal(scope) -> tuple[int, str, str] | None:
        headers = {name.decode("latin-1").lower(): value.decode("latin-1") for name, value in scope["headers"]}
        server = scope.get("server")
        port = server[1] if server else None
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed |= {"127.0.0.1", "localhost"}
        if headers.get("host", "").lower() not in allowed:
            return 403, "forbidden_host", "Dieser Host-Name ist nicht erlaubt."
        method = scope.get("method", "GET")
        websocket = scope["type"] == "websocket"
        origin = headers.get("origin")
        if origin is not None and (websocket or method not in _READ_METHODS):
            if origin.lower() not in {f"http://{host}" for host in allowed}:
                return 403, "forbidden_origin", "Anfragen von dieser Herkunft sind nicht erlaubt."
        if websocket or method in _READ_METHODS:
            return None
        length = headers.get("content-length")
        if length is not None and (not length.isdigit() or len(length) > 18):
            return 400, "bad_request", "Ungültige Content-Length."
        if length is not None and int(length) > MAX_BODY_BYTES:
            return 413, "too_large", "Die Anfrage ist zu groß."
        has_body = "transfer-encoding" in headers or (length is not None and int(length) > 0)
        if has_body and headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return 415, "bad_content_type", "Der Inhalt muss application/json sein."
        return None


def create_app(runtime: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="Werkhalle MES", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        return _error(exc.status, exc.code, exc.message, **exc.extra)

    @app.exception_handler(PlantNotConnected)
    async def _not_connected(_request: Request, _exc: PlantNotConnected):
        return _error(503, "not_connected", "Die Verbindung zur Anlage ist getrennt.")

    @app.exception_handler(RequestValidationError)
    async def _bad_request(_request: Request, exc: RequestValidationError):
        errors = exc.errors()
        detail = errors[0].get("msg", "") if errors else ""
        return _error(400, "bad_request", f"Ungültige Anfrage: {detail}")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return _error(404, "not_found", "Nicht gefunden.")
        return _error(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception):
        logger.exception("request failed", exc_info=exc)
        return _error(500, "internal", "Interner Fehler im MES. Details stehen im Protokoll.")

    @app.middleware("http")
    async def _static_not_cached(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/api"):
            # The UI has no build step and no version in its URLs, so a cached module would outlive an update.
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(LocalOnly)

    # --- state and catalogue ---

    @app.get("/api/health")
    async def health():
        return _json(runtime.health())

    @app.get("/api/state")
    async def state():
        return Response(content=runtime.state_text(), media_type="application/json")

    @app.get("/api/catalog")
    async def get_catalog():
        return _json(runtime.catalog_json())

    # --- orders ---

    @app.get("/api/orders")
    async def orders():
        return _json(runtime.orders_json())

    @app.post("/api/orders")
    async def create_order(request: Request):
        return _json(runtime.create_order(await _body(request)))

    # --- plan ---

    @app.get("/api/plan")
    async def plan():
        return _json(runtime.plan_json())

    @app.post("/api/plan/solve")
    async def plan_solve(request: Request):
        return _json(runtime.start_solve(await _body(request)))

    @app.get("/api/plan/candidates/{candidates_id}")
    async def plan_candidates(candidates_id: str):
        return _json(runtime.candidates_json(candidates_id))

    @app.post("/api/plan/apply")
    async def plan_apply(request: Request):
        return _json(runtime.apply_candidates(await _body(request)))

    # --- proposals ---

    @app.get("/api/proposals")
    async def proposals():
        return _json(runtime.proposals_json())

    @app.post("/api/proposals/{proposal_id}/accept")
    async def accept(proposal_id: str, request: Request):
        solver = (await _body(request)).get("solver")
        if solver is not None and not isinstance(solver, str):
            raise ApiError(400, "bad_request", "solver muss ein Text sein.")
        return _json(runtime.accept_proposal(proposal_id, solver))

    @app.post("/api/proposals/{proposal_id}/reject")
    async def reject(proposal_id: str):
        return _json(runtime.reject_proposal(proposal_id))

    # --- machines, alarms, plant ---

    @app.post("/api/machines/{machine_id}/fault")
    async def machine_fault(machine_id: str, request: Request):
        return _json(await runtime.inject_fault(machine_id, await _body(request)))

    @app.post("/api/machines/{machine_id}/repair")
    async def machine_repair(machine_id: str):
        return _json(await runtime.repair(machine_id))

    @app.post("/api/alarms/{alarm_id}/ack")
    async def alarm_ack(alarm_id: str):
        return _json(await runtime.ack_alarm(alarm_id))

    @app.get("/api/alarms")
    async def alarms(limit: int = 200):
        return _json(runtime.alarms_json(_limit(limit)))

    @app.post("/api/plant/settings")
    async def settings(request: Request):
        return _json(await runtime.apply_settings(await _body(request)))

    @app.get("/api/log")
    async def log(limit: int = 200):
        return _json(runtime.log_json(_limit(limit)))

    # --- OEE and energy ---

    @app.get("/api/oee")
    async def oee(window: str = "start"):
        return _json(runtime.oee_json(window))

    @app.get("/api/energy")
    async def energy():
        return _json(runtime.energy_json())

    # --- traceability ---

    @app.get("/api/trace/search")
    def trace_search(q: str = ""):
        return _json(runtime.require_trace().search(q))

    @app.get("/api/trace/recent")
    def trace_recent(limit: int = 20):
        return _json(runtime.require_trace().recent(_limit(limit)))

    @app.get("/api/trace/graph/{node_id}")
    def trace_graph(node_id: str):
        graph = runtime.require_trace().graph(node_id)
        if graph is None:
            raise ApiError(404, "not_found", f"{node_id} ist nicht bekannt.")
        return _json(graph)

    # --- twin ---

    @app.post("/api/twin/run")
    async def twin_run(request: Request):
        return _json(runtime.start_twin(await _body(request)))

    @app.get("/api/twin/runs/{run_id}")
    async def twin_get(run_id: str):
        # With frames this is over a megabyte, and the loop also runs the OPC UA consumer and the dispatcher.
        return await asyncio.to_thread(_json, runtime.twin_json(run_id))

    @app.post("/api/twin/runs/{run_id}/apply")
    async def twin_apply(run_id: str, request: Request):
        return _json(runtime.apply_twin(run_id, await _body(request)))

    # --- OPC UA monitor ---

    @app.get("/api/opcua/nodes")
    async def opcua_nodes():
        return _json(runtime.client.nodes_table())

    @app.get("/api/opcua/changes")
    async def opcua_changes(limit: int = 200):
        return _json(runtime.client.recent_changes(_limit(limit)))

    @app.get("/api/opcua/methods")
    async def opcua_methods(limit: int = 200):
        return _json(runtime.client.method_calls(_limit(limit)))

    # --- WebSocket ---

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        client = WsClient()
        client.latest = runtime.state_message()
        client.wake.set()
        runtime.add_client(client)
        sender = asyncio.create_task(_send_loop(socket, client), name="ws-send")
        try:
            # The client sends nothing; this only notices it leaving, so the sender does not wait for a send to fail.
            while True:
                receive = asyncio.create_task(socket.receive())
                done, _ = await asyncio.wait({receive, sender}, return_when=asyncio.FIRST_COMPLETED)
                if sender in done:
                    receive.cancel()
                    return
                if receive.result().get("type") == "websocket.disconnect":
                    return
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            runtime.remove_client(client)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            try:
                await asyncio.wait_for(socket.close(), 1.0)
            except (RuntimeError, asyncio.TimeoutError, WebSocketDisconnect):
                pass

    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


async def _send_loop(socket: WebSocket, client: WsClient) -> None:
    """One sender per client: discrete messages in order, then only the newest state. A client that stalls is dropped."""
    try:
        while True:
            await client.wake.wait()
            client.wake.clear()
            while client.queue:
                await asyncio.wait_for(socket.send_text(client.queue.popleft()), WS_SEND_TIMEOUT_S)
            if client.latest is not None:
                message, client.latest = client.latest, None
                await asyncio.wait_for(socket.send_text(message), WS_SEND_TIMEOUT_S)
    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
        return


__all__ = ["create_app"]
