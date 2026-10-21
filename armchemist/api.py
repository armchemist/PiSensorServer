"""NewUI용 API. 채팅은 계획만 생성하며 장비 실행은 확인된 계획의 /runs 요청에서 시작한다."""
from __future__ import annotations

import json
import hashlib
import os
import threading
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .mock import MockAct, MockJudge, MockRail
from .planning import DemoPlanner, Plan, Step, compile_plan
from .registry import CAPABILITIES, INITIAL_FACTS, demo_actions
from .runtime import Runner
from .storage import LocalGraph, Neo4jGraph, Repository, subset, validate_graph


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20000)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=100)
    state: dict = Field(default_factory=dict)


class PlanRequest(BaseModel):
    goal: str = Field(max_length=20000)
    actions: list[str] = Field(min_length=1, max_length=30)
    graphSnapshotId: str


class RunRequest(BaseModel):
    plan_id: str
    version: int
    graphSnapshotId: str
    confirmed: bool


def decode_plan(value):
    return Plan(**{**value, "steps": tuple(Step(**s) for s in value["steps"]),
                   "actions": tuple(value["actions"]),
                   "initial_facts": tuple(tuple(v) for v in value["initial_facts"])})


def create_app(*, repository=None, runner=None, planner=None, graph=None, config_fingerprint=""):
    repo = repository or Repository()
    graph = graph or LocalGraph(repo)
    runner = runner or Runner(MockAct(), MockRail(), MockJudge(), demo_actions())
    planner = planner or DemoPlanner()
    pool, guard = ThreadPoolExecutor(max_workers=1), threading.Lock()
    active = {"id": None, "cancel": None}
    previous_event = runner.on_event
    try:
        runner.facts = repo.get("state", "world")["facts"]
    except KeyError:
        pass
    if any(r["status"] == "unknown" and not r.get("reconciled") for r in repo.all("run")):
        runner.facts = None

    def save_event(report):
        repo.put("run", report["id"], report)
        if previous_event:
            previous_event(report)

    runner.on_event = save_event

    @asynccontextmanager
    async def lifespan(app):
        yield
        with guard:
            if active["cancel"]:
                active["cancel"].set()
        pool.shutdown(wait=True)
        if hasattr(graph, "driver"):
            graph.driver.close()
        if hasattr(runner.act, "ros"):
            runner.act.ros.close()

    app = FastAPI(title="ARMchemist Agent", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
    app.state.repository, app.state.runner = repo, runner

    def get(kind, key):
        try:
            return repo.get(kind, key)
        except KeyError:
            raise HTTPException(404, "기록을 찾을 수 없습니다")

    def make_plan(goal, names, gid):
        if not gid:
            raise ValueError("그래프 스냅샷이 필요합니다")
        value = get("graph", gid)
        experiments = [n for n in value["nodes"] if "Experiment" in n["labels"]
                       and n["properties"].get("kind") == "draft"]
        if len(experiments) != 1 or experiments[0]["properties"].get("actions") != list(names):
            raise ValueError("확정 그래프의 ACT 목록과 계획이 일치하지 않습니다")
        if runner.facts is None:
            raise ValueError("현재 물체 상태가 불명확합니다. 장비 상태 확인이 필요합니다")
        plan = compile_plan(goal, names, gid, initial_facts=runner.facts)
        # 같은 확정 구성의 재요청/새로고침으로 별도의 실행 권한을 만들지 않는다.
        stable = json.dumps([gid, names, plan.initial_facts, config_fingerprint], sort_keys=True, ensure_ascii=False)
        plan = replace(plan, id=hashlib.sha256(stable.encode()).hexdigest()[:32],
                       config_fingerprint=config_fingerprint)
        repo.put("plan", plan.id, plan.to_dict())
        return plan

    @app.get("/health")
    def health():
        return {"ok": True, "mode": "live" if runner.live else "mock", "graph_source": graph.source}

    @app.get("/capabilities")
    def capabilities():
        return {"actions": [{**c.to_dict(), "configured": c.id in runner.actions,
                             "mean_duration_s": runner.actions[c.id].mean_duration_s if c.id in runner.actions else None}
                            for c in CAPABILITIES.values()]}

    @app.get("/devices")
    def devices():
        if not runner.live:
            return {"mode": "mock", "rail_station": runner.rail.station(), "act_paused": runner.act.paused}
        return {"mode": "live", "rail": runner.rail.telemetry(), "act": runner.act._control("status")}

    @app.get("/state")
    def world_state():
        return {"facts": runner.facts, "requires_reconciliation": runner.facts is None}

    @app.post("/state/reconcile")
    def reconcile(body: dict):
        facts = body.get("facts", {})
        allowed = {"left": {"empty", "bottle"}, "right": {"empty", "cap", "cylinder"},
                   "cap": {"open", "closed"}}
        if body.get("observed") is not True or set(facts) != set(INITIAL_FACTS) \
                or any(facts[k] not in values for k, values in allowed.items()):
            raise HTTPException(422, "실제로 확인한 물체 상태가 필요합니다")
        with guard:
            if active["id"]:
                raise HTTPException(409, "실행 중에는 상태를 변경할 수 없습니다")
            # 장비 정지 자체도 확인한 뒤에만 사용자의 물체 상태 확인을 받는다.
            if runner.live:
                runner.act.pause()
                runner.rail.station()
            runner.facts = dict(facts)
            repo.put("state", "world", {"facts": runner.facts})
            for old in repo.all("run"):
                if old["status"] == "unknown":
                    old["reconciled"] = True
                    repo.put("run", old["id"], old)
        return {"facts": runner.facts}

    @app.get("/graph")
    def all_graph():
        return graph.all()

    @app.post("/graph/search")
    def search_graph(body: dict):
        value = graph.all()
        labels = body.get("labels", [])
        text = str(body.get("text", "")).casefold()
        props = body.get("where", {})
        ids = [n["id"] for n in value["nodes"] if (not labels or set(labels) & set(n["labels"]))
               and text in json.dumps(n["properties"], ensure_ascii=False).casefold()
               and all(n["properties"].get(k) == v for k, v in props.items())]
        return subset(value, ids)

    @app.post("/graph/ids")
    def ids_graph(body: dict):
        return subset(graph.all(), body.get("ids", []))

    @app.post("/graph/neighborhood")
    def neighborhood(body: dict):
        value = graph.all()
        ids = set(body.get("ids", []))
        for _ in range(max(0, min(int(body.get("hops", 1)), 5))):
            adjacent = {r[k] for r in value["relationships"] if r["startNode"] in ids or r["endNode"] in ids
                        for k in ("startNode", "endNode")}
            ids |= adjacent
        return subset(value, ids)

    @app.post("/graph/drafts")
    def save_draft(body: dict):
        try:
            validate_graph(body)
            experiments = [n["id"] for n in body["nodes"] if "Experiment" in n["labels"]
                           and n["properties"].get("kind") == "draft"]
            if len(experiments) != 1:
                raise ValueError("실험 초안 노드가 정확히 하나 필요합니다")
            key, value = graph.save(body)
            repo.put("graph", key, value)
            experiments = [n["id"] for n in value["nodes"] if "Experiment" in n["labels"]
                           and n["properties"].get("kind") == "draft"]
            if len(experiments) != 1:
                raise ValueError("실험 초안 노드가 정확히 하나 필요합니다")
            return {"experimentId": experiments[0], "graphSnapshotId": key, "graph": value, "warnings": []}
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.post("/chat")
    def chat(body: ChatRequest):
        def events():
            def event(value):
                return "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"
            yield event({"type": "status", "label": "보유 ACT로 계획 구성 중"})
            try:
                messages = [m.model_dump() for m in body.messages]
                if messages[-1]["role"] != "user":
                    raise ValueError("마지막 메시지는 사용자 요청이어야 합니다")
                draft = body.state.get("draftGraph") or {"nodes": [], "relationships": []}
                validate_graph(draft)
                names = planner.select(messages, {"draft": draft, "inventory": graph.all(),
                                                   "initial_state": runner.facts})
                # ACT 선택 결과도 선행 조건을 검증한 뒤에만 초안에 반영한다.
                if runner.facts is None:
                    raise ValueError("장비 물체 상태를 먼저 확인해야 합니다")
                compile_plan(messages[-1]["content"], names, initial_facts=runner.facts)
                draft = json.loads(json.dumps(draft))
                experiments = [n for n in draft["nodes"] if "Experiment" in n["labels"]
                               and n["properties"].get("kind") == "draft"]
                if not experiments:
                    experiments = [{"id": "experiment-" + uuid4().hex, "labels": ["Experiment"],
                                    "properties": {"name": "ACT 실험", "kind": "draft",
                                                   "description": messages[-1]["content"]}}]
                    draft["nodes"].extend(experiments)
                if len(experiments) != 1:
                    raise ValueError("실험 초안은 하나여야 합니다")
                if body.state.get("confirmedGraphId"):
                    old_id = experiments[0]["id"]
                    experiments[0]["id"] = "experiment-" + uuid4().hex
                    for rel in draft["relationships"]:
                        for key in ("startNode", "endNode"):
                            if rel[key] == old_id:
                                rel[key] = experiments[0]["id"]
                experiments[0]["properties"]["actions"] = names
                gid, draft = graph.save(draft)
                repo.put("graph", gid, draft)
                plan = make_plan(messages[-1]["content"], names, gid)
                yield event({"type": "graph", "graph": draft, "mode": "replace"})
                yield event({"type": "plan", "plan": plan.to_dict()})
                yield event({"type": "text-delta", "text": " → ".join(CAPABILITIES[n].description for n in names)
                             + ". 구성을 확정하고 실행 계획을 확인한 뒤 시작하세요."})
            except Exception as exc:
                yield event({"type": "error", "message": str(exc)})
            yield event({"type": "done"})
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/plans")
    def plans(body: PlanRequest):
        try:
            return make_plan(body.goal, body.actions, body.graphSnapshotId).to_dict()
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/plans/{plan_id}")
    def read_plan(plan_id: str):
        return get("plan", plan_id)

    def execute(plan, run_id, cancel):
        try:
            result = runner.run(plan, cancel, run_id)
            repo.put("run", run_id, result)
            repo.put("state", "world", {"facts": runner.facts})
        except Exception as exc:
            value = repo.get("run", run_id)
            value.update(status="unknown", error=f"실행기 오류: {type(exc).__name__}")
            repo.put("run", run_id, value)
            runner.facts = None
            repo.put("state", "world", {"facts": None})
        finally:
            with guard:
                active.update(id=None, cancel=None)

    @app.post("/runs", status_code=202)
    def runs(body: RunRequest):
        plan = decode_plan(get("plan", body.plan_id))
        if plan.config_fingerprint != config_fingerprint:
            raise HTTPException(409, "배포 설정이 변경되었습니다. 계획을 다시 확인하세요")
        if not body.confirmed or body.version != plan.version or body.graphSnapshotId != plan.graphSnapshotId:
            raise HTTPException(409, "확인한 계획 버전 또는 그래프가 일치하지 않습니다")
        with guard:
            if active["id"]:
                raise HTTPException(409, "이미 실행 중입니다")
            for old in repo.all("run"):
                if old["plan"]["id"] == plan.id:
                    return old  # 브라우저 재전송으로 같은 실험을 반복하지 않는다.
                if runner.live and old["status"] == "unknown" and not old.get("reconciled"):
                    raise HTTPException(409, "이전 실행 결과가 불명확합니다. 장비 상태 확인이 필요합니다")
            if runner.facts is None or dict(plan.initial_facts) != runner.facts:
                raise HTTPException(409, "물체 상태가 변경되었습니다. 상태 확인 후 계획을 다시 생성하세요")
            run_id, cancel = uuid4().hex, threading.Event()
            queued = {"id": run_id, "plan": plan.to_dict(), "status": "queued", "events": [],
                      "mode": "live" if runner.live else "mock"}
            repo.put("run", run_id, queued)
            active.update(id=run_id, cancel=cancel)
            pool.submit(execute, plan, run_id, cancel)
            return queued

    @app.get("/runs/{run_id}")
    def read_run(run_id: str):
        return get("run", run_id)

    @app.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        get("run", run_id)
        with guard:
            if active["id"] == run_id:
                active["cancel"].set()
        return {"id": run_id, "cancel_requested": True}

    @app.get("/reports/{run_id}")
    def report(run_id: str):
        value = get("run", run_id)
        if value["status"] in ("queued", "running"):
            raise HTTPException(409, "아직 실행 중입니다")
        return value

    return app


def from_environment():
    from .settings import Settings
    settings = Settings()
    data = Path(os.getenv("AGENT_DATA_DIR", settings.resolve(settings.raw["data_dir"])))
    data.mkdir(parents=True, exist_ok=True)
    import fcntl
    lock = (data / 'agent.lock').open('a')
    try:
        # 복구 처리 전에 락을 잡아 이중 기동이 실행 중 기록을 unknown으로 바꾸지 못하게 한다.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _configured_app(settings, data, lock)
    except Exception:
        lock.close()
        raise


def _configured_app(settings, data, lock):
    config = settings.raw
    repo = Repository(str(data / "agent.sqlite3"))
    mode = os.getenv("AGENT_MODE", settings.mode)
    if mode not in ("mock", "live"):
        raise ValueError("mode는 mock 또는 live여야 합니다")
    graph_config = config['graph']
    graph = None
    if graph_config['uri']:
        graph = Neo4jGraph(graph_config['uri'], graph_config['user'],
                           settings.secret('graph', 'password_env', required=True), graph_config['database'])
    planner, runner = None, None
    key = settings.secret('openai', 'api_key_env', required=mode == 'live')
    if key:
        from .openai_client import OpenAIHTTP, OpenAIPlanner
        client = OpenAIHTTP(key, timeout=config['openai']['timeout_s'])
        planner = OpenAIPlanner(client, config['openai']['planner_model'])
    if mode == "live":
        from .openai_client import DecisionsJudge
        from .rail import CentralRail
        from .ros import PhysicalAct, Rosbridge
        ros = Rosbridge(settings.robot['rosbridge_url'])
        runner = Runner(PhysicalAct(ros, settings.robot['initial_profile']),
                        CentralRail(config['rail']['central_url'],
                                    api_key=settings.secret('rail', 'api_key_env'),
                                    device_id=config['rail']['device_id'], timeout_s=config['rail']['timeout_s']),
                        DecisionsJudge(client, model=config['openai']['decision_model'],
                                       examples=settings.decision_examples, prompts=settings.decision_prompts),
                        settings.actions, live=True, initial_facts=config['initial_facts'])
    else:
        runner = Runner(MockAct(), MockRail(), MockJudge(),
                        {name: replace(action, mean_duration_s=0.01, duration_source='mock-only')
                         for name, action in settings.actions.items()}, initial_facts=config['initial_facts'])
    api = create_app(repository=repo, runner=runner, planner=planner, graph=graph,
                     config_fingerprint=hashlib.sha256((settings.fingerprint + mode).encode()).hexdigest())
    @asynccontextmanager
    async def lifespan(app):
        try:
            async with api.router.lifespan_context(api):
                yield
        finally:
            lock.close()
    app = FastAPI(lifespan=lifespan)
    app.mount("/api", api)
    ui_dist = Path(__file__).resolve().parents[1] / "ui" / "dist"
    if ui_dist.is_dir():
        app.mount("/", StaticFiles(directory=ui_dist, html=True))
    return app
