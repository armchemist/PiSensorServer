"""계획·실행 기록은 SQLite에 원자적으로 저장한다. 그래프는 선택적으로 Neo4j에 저장한다."""
import hashlib
import json
import sqlite3
import threading
from copy import deepcopy

from .registry import STATIONS


def validate_graph(graph):
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) \
            or not isinstance(graph.get("relationships"), list):
        raise ValueError("잘못된 그래프 형식")
    if len(graph["nodes"]) > 2000 or len(graph["relationships"]) > 5000:
        raise ValueError("그래프 크기 제한 초과")
    ids = set()
    for node in graph["nodes"]:
        if not isinstance(node.get("id"), str) or node["id"] in ids \
                or not isinstance(node.get("labels"), list) or not isinstance(node.get("properties"), dict):
            raise ValueError("잘못되거나 중복된 그래프 노드")
        ids.add(node["id"])
    rel_ids = set()
    for rel in graph["relationships"]:
        if not isinstance(rel.get("id"), str) or rel["id"] in rel_ids \
                or rel.get("startNode") not in ids or rel.get("endNode") not in ids \
                or not isinstance(rel.get("type"), str) or not isinstance(rel.get("properties"), dict):
            raise ValueError("잘못되거나 중복된 그래프 관계")
        rel_ids.add(rel["id"])


def graph_id(graph):
    validate_graph(graph)
    canonical = {key: sorted(graph[key], key=lambda item: item["id"]) for key in ("nodes", "relationships")}
    return "graph-" + hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False,
                                               allow_nan=False).encode()).hexdigest()[:24]


class Repository:
    def __init__(self, path=":memory:", *, recover_runs=True):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, body TEXT, PRIMARY KEY(kind,id))")
        self.db.commit()
        # 프로세스가 죽은 실행을 자동 재개하지 않는다.
        for record in self.all("run") if recover_runs else ():
            if record["status"] in ("running", "queued"):
                record.update(status="unknown", error="서버가 재시작되었습니다. 장비 상태를 확인해야 합니다.")
                self.put("run", record["id"], record)

    def put(self, kind, key, value):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?)", (kind, key, body))

    def get(self, kind, key):
        with self.lock:
            row = self.db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if not row:
            raise KeyError(key)
        return json.loads(row[0])

    def all(self, kind):
        with self.lock:
            rows = self.db.execute("SELECT body FROM records WHERE kind=?", (kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]


class LocalGraph:
    source = "mock"

    def __init__(self, repository):
        self.repository = repository
        try:
            repository.get("graph", "inventory")
        except KeyError:
            repository.put("graph", "inventory", {"nodes": [
                {"id": "station-" + str(i), "labels": ["Location"], "properties": {"name": name}}
                for i, name in enumerate(STATIONS)], "relationships": [], "source": "mock"})

    def all(self):
        return self.repository.get("graph", "inventory")

    def save(self, graph):
        graph = deepcopy(graph)
        key = graph_id(graph)
        graph["source"] = self.source
        self.repository.put("graph", key, graph)
        return key, graph


class Neo4jGraph:
    """NewUI 스냅샷을 Neo4j 노드/엣지로 저장한다. 데이터는 snapshot별로 격리한다."""
    source = "live"

    def __init__(self, uri, user, password, database="neo4j"):
        from neo4j import GraphDatabase
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database
        self.driver.verify_connectivity()

    def all(self):
        with self.driver.session(database=self.database) as session:
            rows = session.run("MATCH (n:ArmchemistNode {snapshot: 'inventory'}) "
                               "RETURN n.node_id AS id, n.labels AS labels, n.properties AS properties")
            nodes = [{"id": r["id"], "labels": r["labels"], "properties": json.loads(r["properties"])} for r in rows]
            rows = session.run("MATCH (a:ArmchemistNode {snapshot: 'inventory'})-[r:ArmchemistEdge]->"
                               "(b:ArmchemistNode {snapshot: 'inventory'}) RETURN r.edge_id AS id, "
                               "r.kind AS type, a.node_id AS startNode, b.node_id AS endNode, r.properties AS properties")
            rels = [{**dict(r), "properties": json.loads(r["properties"])} for r in rows]
        return {"nodes": nodes, "relationships": rels, "source": "live"}

    def save(self, graph):
        key = graph_id(graph)
        with self.driver.session(database=self.database) as session:
            session.execute_write(self._save, key, graph)
        return key, {**deepcopy(graph), "source": "live"}

    @staticmethod
    def _save(tx, key, graph):
        # 내용 해시로 스냅샷을 식별하고 입력값은 Cypher 매개변수로 전달한다.
        for node in graph["nodes"]:
            tx.run("MERGE (n:ArmchemistNode {snapshot:$snapshot, node_id:$id}) "
                   "SET n.labels=$labels, n.properties=$properties", snapshot=key, id=node["id"],
                   labels=node["labels"], properties=json.dumps(node["properties"], ensure_ascii=False))
        for rel in graph["relationships"]:
            tx.run("MATCH (a:ArmchemistNode {snapshot:$snapshot, node_id:$start}), "
                   "(b:ArmchemistNode {snapshot:$snapshot, node_id:$end}) "
                   "MERGE (a)-[r:ArmchemistEdge {edge_id:$id}]->(b) SET r.kind=$kind, r.properties=$properties",
                   snapshot=key, start=rel["startNode"], end=rel["endNode"], id=rel["id"], kind=rel["type"],
                   properties=json.dumps(rel["properties"], ensure_ascii=False))


def subset(graph, ids):
    ids = set(ids)
    return {"nodes": [n for n in graph["nodes"] if n["id"] in ids],
            "relationships": [r for r in graph["relationships"] if r["startNode"] in ids and r["endNode"] in ids],
            "source": graph.get("source", "mock")}
