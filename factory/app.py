from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import quote
import asyncio
import hmac
import secrets
import time

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

from factory.assistant import AGENTS, FactoryAssistant
from factory.config import DOMAINS, OUTPUTS, ROLES, ROOT, Settings, internal_address
from factory.devices import DeviceHub
from factory.operations import Operations, PASS_FIELDS
from factory.permissions import allowed, permission_matrix, role_permissions
from factory.protocol import Actuator, MAX_FRAME
from factory.reports import build_report
from factory.simulator import SCENARIOS, SimulatedDevice
from factory.skills import CATALOG, FactorySkills, build_mcp, call_mcp
from factory.store import Store, day_bounds


COOKIE = "shuyun_session"
SESSION_SECONDS = 12 * 60 * 60
WORD_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Body):
    username: str = Field(min_length=1, max_length=24)
    password: str = Field(min_length=1, max_length=300)

    model_config = ConfigDict(extra="forbid")


class CommandDraft(Body):
    device_id: str = Field(min_length=1, max_length=48)
    actuator: Actuator
    value: Annotated[StrictInt, Field(ge=0, le=100)]


class Question(Body):
    device_id: str = Field(min_length=1, max_length=48)
    agent: Literal["auto", "device", "safety", "energy"] = "device"
    text: str = Field(min_length=1, max_length=360)


class CloseAlert(Body):
    note: str = Field(min_length=2, max_length=160)


class Rules(Body):
    temperature_high: Annotated[float, Field(ge=1, le=80)]
    humidity_low: Annotated[float, Field(ge=0, le=99)]
    humidity_high: Annotated[float, Field(ge=1, le=100)]
    light_low: Annotated[StrictInt, Field(ge=0, le=4095)]

    @model_validator(mode="after")
    def check_humidity(self):
        if self.humidity_low >= self.humidity_high:
            raise ValueError("湿度下限必须小于上限。")
        return self


class Scenario(Body):
    scenario: Literal["normal", "overheat", "dim", "presence", "obstruction", "disconnect"]


class BusinessWrite(Body):
    kind: Literal["production", "inventory"]
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    code: str = Field(min_length=1, max_length=48)
    label: str = Field(min_length=1, max_length=64)
    quantity: Annotated[StrictInt, Field(ge=0, le=100_000_000)]
    source: str = Field(min_length=2, max_length=160)


class HandoffWrite(Body):
    device_id: str = Field(min_length=1, max_length=48)
    reason: str = Field(min_length=2, max_length=240)
    conversation_id: Annotated[StrictInt, Field(gt=0)] | None = None


class PassIssue(Body):
    device_id: str = Field(min_length=1, max_length=48)
    purpose: str = Field(min_length=2, max_length=96)
    minutes: Annotated[StrictInt, Field(ge=1, le=30)] = 5


class PassVerify(Body):
    code: str = Field(min_length=16, max_length=32)


Prompt = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100)]
Guidance = Annotated[str, StringConstraints(strip_whitespace=True, max_length=240)]


class AgentGuidance(Body):
    device: Guidance
    safety: Guidance
    energy: Guidance


class AgentPrompts(Body):
    device: list[Prompt] = Field(min_length=1, max_length=5)
    safety: list[Prompt] = Field(min_length=1, max_length=5)
    energy: list[Prompt] = Field(min_length=1, max_length=5)
    guidance: AgentGuidance | None = None


class SkillInput(Body):
    name: Literal["read_factory_data", "read_business_summary", "read_energy_summary"]
    device_id: str | None = Field(default=None, max_length=48)
    day: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ModelTest(Question):
    compare: bool = False
    input_rate: float = Field(default=0, ge=0, le=10_000)
    output_rate: float = Field(default=0, ge=0, le=10_000)


@dataclass
class Session:
    actor: str
    expires_at: float


def create_app(settings: Settings) -> FastAPI:
    sessions: dict[str, Session] = {}
    mcp = build_mcp(lambda: app.state.skills)
    mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True, max_request_body_size=32768)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.data_dir / "factory.sqlite3", settings.devices, settings.demo)
        hub = DeviceHub(store, settings.devices)
        listener = None
        jobs = []
        try:
            operations = Operations(store, hub, settings.demo)
            listener = await asyncio.start_server(hub.handle, settings.tcp_host, settings.tcp_port, limit=MAX_FRAME)
            tcp_port = listener.sockets[0].getsockname()[1]
            simulators = {}
            if settings.demo:
                for index, config in enumerate(settings.devices):
                    device = SimulatedDevice(config, index, "127.0.0.1", tcp_port)
                    simulators[config.id] = device
                    if store.latest(config.id) is None:
                        now = time.time()
                        for seconds in range(238, 0, -2):
                            at = now - seconds
                            sample = device.sample(at)
                            sample["outputs"]["led"] = 35
                            store.ingest(config.id, sample, at)
                    jobs.append(asyncio.create_task(device.run(), name=f"sim:{config.id}"))
            jobs.append(asyncio.create_task(hub.expire_pending(), name="expire-receipts"))
            app.state.store = store
            app.state.hub = hub
            app.state.tcp_port = tcp_port
            app.state.simulators = simulators
            app.state.operations = operations
            app.state.skills = FactorySkills(store, hub, operations)
            app.state.assistant = FactoryAssistant(store, hub, settings.model, settings.data_dir / "agentica", operations)
            async with mcp.session_manager.run():
                yield
        finally:
            if listener:
                listener.close()
            for job in jobs:
                job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
            await hub.close()
            if listener:
                await listener.wait_closed()
            store.close()

    app = FastAPI(title="蜀云智造", version="1.1.0", lifespan=lifespan)
    app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")

    def store() -> Store:
        return app.state.store

    def hub() -> DeviceHub:
        return app.state.hub

    def operations() -> Operations:
        return app.state.operations

    def same_origin(connection) -> bool:
        origin = connection.headers.get("origin")
        scheme = "https" if connection.url.scheme in ("https", "wss") else "http"
        return origin is None or origin == f"{scheme}://{connection.url.netloc}"

    def resolve_session(token: str | None) -> Session | None:
        session = sessions.get(token) if token else None
        if session and session.expires_at <= time.monotonic():
            sessions.pop(token, None)
            return None
        return session

    def require(request: Request, permission: str | None = None) -> str:
        if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
            raise HTTPException(403, "请从平台页面发起操作。")
        session = resolve_session(request.cookies.get(COOKIE))
        if session is None:
            raise HTTPException(401, "请先登录。")
        if permission and not allowed(session.actor, permission):
            raise HTTPException(403, "该账户无权执行此操作。")
        return session.actor

    def authorize_tool(request: Request):
        if not same_origin(request):
            raise HTTPException(403, "请从平台页面或授权的内网客户端调用工具。")
        provided = request.headers.get("authorization", "")
        if settings.skills.token and hmac.compare_digest(provided.encode(), ("Bearer " + settings.skills.token).encode()):
            return
        require(request, "skills.manage")

    async def protected_mcp(scope, receive, send):
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        try:
            authorize_tool(Request(scope, receive))
        except HTTPException as error:
            await JSONResponse({"detail": error.detail}, status_code=error.status_code)(scope, receive, send)
            return
        await mcp_app(scope, receive, send)

    app.mount("/toolkit", protected_mcp)

    def device_or_404(device_id: str):
        config = hub().config.get(device_id)
        if config is None:
            raise HTTPException(404, "设备未登记。")
        return config

    def device_snapshot(config) -> dict:
        return {
            "id": config.id, "name": config.name, "domain": config.domain,
            "kind": "simulator" if settings.demo else "esp32", **hub().current(config.id),
        }

    def snapshot() -> dict:
        return {
            "now": time.time(), "devices": [device_snapshot(config) for config in settings.devices],
            "alerts": store().alerts(40),
            "events": store().rows("SELECT * FROM event ORDER BY at DESC,id DESC LIMIT 25"),
            "commands": store().rows("SELECT * FROM command ORDER BY created_at DESC LIMIT 30"),
            "rules": store().rules,
        }

    @app.get("/", include_in_schema=False)
    @app.get("/screen", include_in_schema=False)
    async def index():
        return FileResponse(ROOT / "web" / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "mode": "demo" if settings.demo else "factory", "tcp_port": app.state.tcp_port}

    @app.get("/api/session")
    async def session_info(request: Request):
        session = resolve_session(request.cookies.get(COOKIE))
        actor = session.actor if session else None
        return {
            "user": actor, "demo": settings.demo, "model_enabled": bool(settings.model),
            "domains": DOMAINS, "outputs": OUTPUTS, "roles": ROLES, "agents": {"auto": "自动分工", **AGENTS},
            "scenarios": SCENARIOS if settings.demo else {},
            "permissions": role_permissions(actor), "prompts": operations().prompts(),
        }

    failures: dict[str, tuple[int, float]] = {}

    @app.post("/api/login")
    async def login(body: Login, request: Request, response: Response):
        if not same_origin(request):
            raise HTTPException(403, "请从平台页面登录。")
        who = request.client.host if request.client else "local"
        now = time.monotonic()
        for key, (_, until) in tuple(failures.items()):
            if until < now:
                failures.pop(key, None)
        attempts, until = failures.get(who, (0, now + 60))
        if attempts >= 8:
            raise HTTPException(429, "尝试次数过多，请稍后再登录。")
        password = settings.accounts.get(body.username)
        if password is None or not hmac.compare_digest(password.encode(), body.password.encode()):
            failures[who] = (attempts + 1, until)
            raise HTTPException(401, "用户名或密码不正确。")
        failures.pop(who, None)
        for token, session in tuple(sessions.items()):
            if session.expires_at <= now or token == request.cookies.get(COOKIE):
                sessions.pop(token)
        token = secrets.token_urlsafe(32)
        sessions[token] = Session(body.username, now + SESSION_SECONDS)
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https")
        response.headers["Cache-Control"] = "no-store"
        store().audit(body.username, "登录", "网页登录成功")
        return {"user": body.username}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        actor = require(request)
        sessions.pop(request.cookies.get(COOKIE), None)
        response.delete_cookie(COOKIE)
        store().audit(actor, "退出", "网页登录已退出")
        return {"ok": True}

    @app.get("/api/state")
    async def state(request: Request):
        require(request, "monitor.read")
        return snapshot()

    @app.websocket("/ws")
    async def live(socket: WebSocket):
        token = socket.cookies.get(COOKIE)
        session = resolve_session(token)
        if not same_origin(socket) or not session or not allowed(session.actor, "monitor.read"):
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            while resolve_session(token):
                await socket.send_json({"type": "state", "data": snapshot()})
                try:
                    await asyncio.wait_for(socket.receive_text(), 1.5)
                except TimeoutError:
                    pass
                else:
                    # Push-only channel. Ignore incoming data and keep a fixed push rate.
                    await asyncio.sleep(1.5)
            await socket.close(code=1008)
        except (WebSocketDisconnect, OSError):
            pass

    @app.get("/api/devices/{device_id}/trend")
    async def trend(device_id: str, request: Request, minutes: int = Query(default=10, ge=1, le=60)):
        require(request, "monitor.read")
        device_or_404(device_id)
        return store().trend(device_id, minutes * 60)

    @app.post("/api/commands", status_code=201)
    async def preview(body: CommandDraft, request: Request):
        actor = require(request, "command.draft")
        device_or_404(body.device_id)
        try:
            return hub().draft(body.device_id, body.actuator, body.value, actor)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/commands/{command_id}")
    async def command_status(command_id: str, request: Request):
        require(request, "records.read")
        command = store().command(command_id)
        if command is None:
            raise HTTPException(404, "找不到该指令。")
        return command

    @app.post("/api/commands/{command_id}/confirm")
    async def confirm(command_id: str, request: Request):
        actor = require(request, "command.confirm")
        command = store().command(command_id)
        if command is None:
            raise HTTPException(404, "找不到该指令。")
        if actor not in ("admin", command["actor"]):
            raise HTTPException(403, "只能确认自己预览的指令。")
        return await hub().confirm(command, actor)

    @app.post("/api/assistant")
    async def ask(body: Question, request: Request):
        actor = require(request, "assistant.ask")
        device_or_404(body.device_id)
        return await app.state.assistant.answer(body.text, body.device_id, body.agent, actor, allowed(actor, "command.draft"))

    @app.put("/api/rules")
    async def update_rules(body: Rules, request: Request):
        actor = require(request, "rules.manage")
        store().set_rules(body.model_dump(), actor)
        return store().rules

    @app.post("/api/alerts/{alert_id}/close")
    async def close_alert(alert_id: int, body: CloseAlert, request: Request):
        actor = require(request, "alert.close")
        try:
            return store().close_alert(alert_id, actor, body.note)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/demo/{device_id}/scenario")
    async def set_scenario(device_id: str, body: Scenario, request: Request):
        actor = require(request, "demo.run")
        simulator = app.state.simulators.get(device_id)
        if simulator is None:
            raise HTTPException(404, "该设备没有模拟操作。")
        simulator.set_scenario(body.scenario)
        store().audit(actor, "切换模拟场景", f"{device_id} · {SCENARIOS[body.scenario]}")
        return {"scenario": body.scenario}

    @app.get("/api/business")
    async def business_data(request: Request, day: str | None = None):
        require(request, "records.read")
        try:
            return operations().business(day)
        except ValueError as exc:
            raise HTTPException(422, "日期须使用 YYYY-MM-DD。") from exc

    @app.put("/api/business")
    async def update_business(body: BusinessWrite, request: Request):
        actor = require(request, "business.write")
        try:
            return operations().save_business(**body.model_dump(), actor=actor)
        except ValueError as exc:
            raise HTTPException(422, "请使用有效的统计日期。") from exc

    @app.get("/api/decisions/{device_id}")
    async def suggestions(device_id: str, request: Request):
        require(request, "monitor.read")
        device_or_404(device_id)
        return operations().suggestions(device_id)

    @app.post("/api/decisions/{device_id}/{plan_id}/adopt")
    async def adopt(device_id: str, plan_id: str, request: Request):
        actor = require(request, "decision.adopt")
        device_or_404(device_id)
        try:
            return operations().adopt(device_id, plan_id, actor)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/handoffs")
    async def handoff_list(request: Request):
        require(request, "handoff.manage")
        return operations().handoffs()

    @app.post("/api/handoffs", status_code=201)
    async def create_handoff(body: HandoffWrite, request: Request):
        actor = require(request, "handoff.manage")
        device_or_404(body.device_id)
        try:
            return operations().handoff(body.device_id, actor, body.reason, body.conversation_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/handoffs/{ticket_id}/resolve")
    async def resolve_handoff(ticket_id: int, body: CloseAlert, request: Request):
        actor = require(request, "handoff.manage")
        try:
            return operations().resolve_handoff(ticket_id, actor, body.note)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/alerts/{alert_id}/review")
    async def follow_up_alert(alert_id: int, body: CloseAlert, request: Request):
        actor = require(request, "alert.review")
        try:
            return operations().review_alert(alert_id, actor, body.note)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/access")
    async def access(request: Request):
        require(request, "pass.manage")
        return operations().access()

    @app.post("/api/access/issue", status_code=201)
    async def issue(body: PassIssue, request: Request):
        actor = require(request, "pass.manage")
        try:
            data = operations().issue_pass(body.device_id, actor, body.purpose, body.minutes)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return JSONResponse(data, status_code=201, headers={"Cache-Control": "no-store"})

    @app.post("/api/access/verify")
    async def verify(body: PassVerify, request: Request):
        actor = require(request, "pass.manage")
        try:
            return operations().verify_pass(body.code, actor)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/access/{pass_id}/confirm")
    async def confirm_pass(pass_id: str, request: Request):
        actor = require(request, "pass.manage")
        try:
            return operations().confirm_pass(pass_id, actor)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/admin/tools")
    async def tool_settings(request: Request):
        require(request, "skills.manage")
        return {
            **permission_matrix(), "prompts": operations().prompts(), "guidance": operations().guidance(),
            "skills": CATALOG, "remote_mcp": bool(settings.skills.remote_url),
            "model": settings.model.id if settings.model else None,
        }

    @app.put("/api/admin/prompts")
    async def update_prompts(body: AgentPrompts, request: Request):
        actor = require(request, "skills.manage")
        return operations().save_prompts(body.model_dump(exclude={"guidance"}), actor,
                                         body.guidance.model_dump() if body.guidance else None)

    @app.post("/api/skills/run")
    async def run_skill(body: SkillInput, request: Request):
        authorize_tool(request)
        try:
            return app.state.skills.run(body.name, body.device_id, body.day)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/skills/mcp/{target}")
    async def test_mcp(target: Literal["local", "remote"], body: SkillInput, request: Request):
        actor = require(request, "skills.manage")
        if target == "remote":
            url = settings.skills.remote_url
            if not url:
                raise HTTPException(409, "请先在 .env 登记受信的内网 MCP 服务。")
            headers = {"Authorization": "Bearer " + settings.skills.remote_token} if settings.skills.remote_token else {}
        else:
            address, port = request.scope.get("server") or ("127.0.0.1", 8000)
            if address in ("", "0.0.0.0", "::"):
                address = "127.0.0.1"
            if not internal_address(address):
                raise HTTPException(409, "本机 MCP 测试需通过内网地址运行。")
            host = f"[{address}]" if ":" in address else address
            url = f"{request.url.scheme}://{host}:{port}/toolkit/mcp"
            headers = {"Cookie": f"{COOKIE}={request.cookies[COOKIE]}"}
        arguments = {}
        if CATALOG[body.name]["device"] and body.device_id:
            arguments["device_id"] = body.device_id
        if CATALOG[body.name]["day"] and body.day:
            arguments["day"] = body.day
        try:
            result = await call_mcp(url, headers, body.name, arguments)
        except Exception as exc:
            raise HTTPException(502, "MCP 调用失败，请检查服务状态、凭据和工具参数。") from exc
        store().audit(actor, "测试只读 MCP 工具", f"{target} · {body.name}")
        return result

    @app.post("/api/admin/model-test")
    async def test_model(body: ModelTest, request: Request):
        actor = require(request, "skills.manage")
        device_or_404(body.device_id)
        if body.compare and not settings.model:
            raise HTTPException(409, "请先配置 Agentica 的模型接口。")
        args = (body.text, body.device_id, body.agent, actor, False)
        assistant = app.state.assistant
        started = time.monotonic()
        baseline = await assistant.answer(*args, model_enabled=False, save=False)
        baseline.update(model="本地数据工具", elapsed_ms=round((time.monotonic() - started) * 1000), estimated_cost=0)
        results = [baseline]
        if body.compare:
            started = time.monotonic()
            measured = await assistant.answer(*args, save=False)
            usage = measured["usage"]
            cost = (usage["input"] * body.input_rate + usage["output"] * body.output_rate) / 1_000_000
            measured.update(model=settings.model.id, elapsed_ms=round((time.monotonic() - started) * 1000),
                            estimated_cost=round(cost, 8) if measured["engine"] == "agentica" else None)
            results.append(measured)
        store().audit(actor, "测试模型问数", f"{body.device_id} · {len(results)} 种运行方式")
        return {"results": results}

    @app.get("/api/records")
    async def records(
        request: Request, kind: Literal["events", "alerts", "commands", "conversations", "passes", "audit"] = "events",
        q: str = Query(default="", max_length=120),
    ):
        permission = {"audit": "audit.read", "passes": "pass.manage"}.get(kind, "records.read")
        actor = require(request, permission)
        queries = {
            "events": ("event", "at", ("device_id", "kind", "message")),
            "alerts": ("alert", "opened_at", ("device_id", "metric", "note")),
            "commands": ("command", "created_at", ("id", "device_id", "actor", "actuator", "status", "detail")),
            "conversations": ("conversation", "at", ("device_id", "actor", "question", "answer")),
            "passes": ("access_pass", "issued_at", ("id", "device_id", "purpose", "status", "detail")),
            "audit": ("audit", "at", ("actor", "action", "detail")),
        }
        table, timestamp, fields = queries[kind]
        conditions, args = [], []
        if q.strip():
            conditions.append("(" + " OR ".join(f"instr(lower(coalesce({field},'')),?)>0" for field in fields) + ")")
            args.extend([q.strip().lower()] * len(fields))
        if kind == "conversations" and actor != "admin":
            conditions.append("actor=?")
            args.append(actor)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        fields = PASS_FIELDS if kind == "passes" else "*"
        if kind == "alerts":
            fields += ",(SELECT note FROM alert_review WHERE alert_id=alert.id) AS review_note"
        return store().rows(f"SELECT {fields} FROM {table}{where} ORDER BY {timestamp} DESC,id DESC LIMIT 150", tuple(args))

    @app.get("/api/summary")
    async def summary(request: Request, day: str | None = None, device_id: str | None = None):
        require(request, "screen.read")
        if device_id:
            device_or_404(device_id)
        try:
            data = store().dashboard_summary(device_id, day)
        except ValueError as exc:
            raise HTTPException(422, "日期须使用 YYYY-MM-DD。") from exc
        business = operations().business(data["day"]) if not device_id else None
        return {
            **data, "online": sum(hub().current(c.id)["fresh"] for c in settings.devices),
            "registered": len(settings.devices),
            "business": business["totals"] if business else None,
            "business_counts": business["counts"] if business else None,
        }

    @app.get("/api/reports/{kind}")
    async def report(kind: Literal["inspection", "energy"], request: Request, day: str | None = None,
                     device_id: str | None = None):
        actor = require(request, "reports.export")
        name = device_or_404(device_id).name if device_id else "所有登记设备"
        try:
            label, start, end = day_bounds(day)
        except ValueError as exc:
            raise HTTPException(422, "日期须使用 YYYY-MM-DD。") from exc
        data = store().dashboard_summary(device_id, label)
        alarms = []
        if kind == "inspection":
            where = " AND a.device_id=?" if device_id else ""
            args = (start, end, device_id) if device_id else (start, end)
            alarms = store().rows(
                f"""SELECT a.*,d.name AS device_name FROM alert a JOIN device d ON a.device_id=d.id
                    WHERE a.opened_at>=? AND a.opened_at<?{where} ORDER BY a.opened_at DESC LIMIT 16""", args,
            )
        content = await run_in_threadpool(build_report, kind, data, name, settings.demo, alarms)
        title = "巡检报告" if kind == "inspection" else "能耗估算报告"
        filename = quote(f"{title}-{label}.docx")
        store().audit(actor, "导出报告", f"{title} · {label} · {name}")
        return Response(content, media_type=WORD_MEDIA_TYPE, headers={
            "Content-Disposition": f"attachment; filename=\"{kind}-{label}.docx\"; filename*=UTF-8''{filename}",
            "Cache-Control": "no-store",
        })

    return app
