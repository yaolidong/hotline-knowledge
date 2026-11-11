"""KAG 子图 → OpenSPG writerGraph；读取使用同一 Neo4j 的参数化查询。"""

import hashlib
import json

import httpx
from neo4j import GraphDatabase, Query, READ_ACCESS

from hotline.models import Chunk, Extraction, stable_id
from hotline.schema import spg_name
from hotline.settings import ServiceError, Settings
from hotline.store import dumps
from hotline.semantics import canonical_name, entity_sequence


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def build_subgraph(revision: dict, namespace: str) -> dict:
    try:
        from kag.interface.common.model.sub_graph import Edge, Node, SubGraph
    except ImportError as exc:
        raise ServiceError("图发布需要安装固定版本 KAG", 503) from exc
    extraction = Extraction.model_validate(revision["extraction"])
    chunks = [Chunk.model_validate(c) for c in revision["chunks"]]
    rid = revision["id"]
    region = revision["metadata"].get("region", "")
    nodes, edges = {}, {}

    def node(key, name, kind, properties):
        props = {"recordId": rid, "entityType": kind, "region": region, **properties}
        props["contentHash"] = digest({"id": key, "name": name, "type": kind, "properties": props})
        value = Node(key, name, f"{namespace}.{kind}", props)
        nodes[key] = value
        return value

    def edge(source, relation, target, evidence):
        key = stable_id(source.id, relation, target.id)
        props = {"recordId": rid, "evidence": dumps(evidence), "factId": key, "relationType": relation}
        props["contentHash"] = digest(props)
        edges[key] = Edge(key, source, target, spg_name(relation), props)

    document = node(
        stable_id(rid, "document"),
        revision["metadata"].get("title") or revision["filename"],
        "SourceDocument",
        {
            "metadata": dumps(
                {
                    **revision["metadata"],
                    "document_id": revision["document_id"],
                    "version": revision["number"],
                    "sha256": revision["sha256"],
                }
            )
        },
    )
    chunk_nodes = {}
    for chunk in chunks:
        chunk_nodes[chunk.id] = node(
            chunk.id,
            chunk.section or f"原文 {chunk.start}",
            "Chunk",
            {"content": chunk.text, "metadata": dumps(chunk.model_dump(exclude={"text"}))},
        )
        edge(document, "HAS_CHUNK", chunk_nodes[chunk.id], [])
    entity_nodes = {}
    for entity in extraction.entities:
        entity_nodes[entity.id] = node(
            stable_id(rid, entity.id),
            entity.canonical_name or entity.name,
            entity.type,
            {
                "attrs": dumps(entity.attributes),
                "aliases": dumps(entity.aliases),
                "rule": dumps(entity.rule),
                "evidence": dumps([e.model_dump() for e in entity.evidence]),
                "metadata": dumps(
                    {
                        "raw_name": entity.name,
                        "canonical_name": entity.canonical_name or canonical_name(entity.name),
                        "display_name": entity.display_name,
                        "sequence": entity_sequence(entity),
                        "sequence_basis": entity.sequence_basis,
                        "schema_version": extraction.schema_version,
                    }
                ),
                "canonicalKey": stable_id(
                    region,
                    entity.type,
                    entity.canonical_name or canonical_name(entity.name),
                    entity.attributes.get("level", ""),
                ),
            },
        )
        grouped = {}
        for evidence in entity.evidence:
            grouped.setdefault(evidence.chunk_id, []).append(evidence.model_dump())
        for cid, evidence in grouped.items():
            edge(entity_nodes[entity.id], "SUPPORTED_BY", chunk_nodes[cid], evidence)
    for relation in extraction.relations:
        edge(
            entity_nodes[relation.source],
            relation.type,
            entity_nodes[relation.target],
            [e.model_dump() for e in relation.evidence],
        )
    return SubGraph(list(nodes.values()), list(edges.values())).to_dict()


def decode_scalar(value):
    if isinstance(value, str) and value.startswith('"'):
        return json.loads(value)
    return value


def unpack_node(properties: dict) -> dict:
    # 锁定版本 Neo4jSinkWriter 对非数组值调用 JSON.toJSONString（id 由底层单独写入）。
    encoded = str(properties.get("recordId", "")).startswith('"')
    result = {
        key: decode_scalar(value) if encoded and key != "id" else value for key, value in properties.items()
    }
    for key, default in (("attrs", {}), ("aliases", []), ("evidence", []), ("rule", None), ("metadata", {})):
        value = result.get(key)
        result[key] = (
            json.loads(value) if isinstance(value, str) else (value if value is not None else default)
        )
    return result


class GraphStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._driver = None

    def _stored(self, value):
        return (
            json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if self.settings.openspg_scalar_encoding == "json"
            else value
        )

    def _fragment(self, value):
        return self._stored(value)[1:-1] if self.settings.openspg_scalar_encoding == "json" else value

    @property
    def driver(self):
        s = self.settings
        if not s.neo4j_password.get_secret_value():
            raise ServiceError("图数据库尚未配置：请设置 NEO4J_PASSWORD", 503)
        if self._driver is None:
            self._driver = GraphDatabase.driver(
                s.neo4j_uri,
                auth=(s.neo4j_user, s.neo4j_password.get_secret_value()),
                connection_timeout=5,
                max_transaction_retry_time=5,
            )
        return self._driver

    def close(self):
        if self._driver:
            self._driver.close()

    def _read(self, cypher: str, **params):
        try:
            with self.driver.session(
                database=self.settings.neo4j_database, default_access_mode=READ_ACCESS
            ) as session:
                return [row.data() for row in session.run(Query(cypher, timeout=10), **params)]
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("图数据库查询失败，请检查连接、权限与服务状态", 503) from exc

    def health(self):
        self._read("RETURN 1 AS ok")
        return True

    def publish(self, revision: dict):
        s = self.settings
        if s.openspg_project_id <= 0:
            raise ServiceError("尚未设置 OpenSPG 项目 ID", 503)
        graph = build_subgraph(revision, s.namespace)
        # 先确认读取端可用，随后写入并回读校验，防止 HTTP 200 被误当作完整成功。
        self.health()
        body = {
            "projectId": s.openspg_project_id,
            "operation": "UPSERT",
            "subGraph": graph,
            "enableLeadTo": False,
        }
        if s.openspg_token.get_secret_value():
            body["token"] = s.openspg_token.get_secret_value()
        try:
            response = httpx.post(
                s.openspg_url.rstrip("/") + "/public/v1/graph/writerGraph",
                json=body,
                timeout=120,
                trust_env=s.http_trust_env,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("success") is not True or result.get("result") is not True:
                raise ServiceError("OpenSPG 未确认图写入成功", 502)
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("OpenSPG 写入失败，知识未发布，可在恢复服务后重试", 502) from exc
        self.verify(revision["id"], graph)

    def verify(self, revision_id, graph):
        # namespace 只来自经过严格校验的服务配置，用户输入全部参数绑定。
        rows = self._read(
            "MATCH (n) WHERE n.recordId=$rid AND any(l IN labels(n) WHERE l STARTS WITH $prefix) "
            "RETURN n.id AS id,n.contentHash AS hash",
            rid=self._stored(revision_id),
            prefix=self.settings.namespace + ".",
        )
        actual = {r["id"]: decode_scalar(r["hash"]) for r in rows}
        expected = {n["id"]: n["properties"]["contentHash"] for n in graph["resultNodes"]}
        rows = self._read(
            "MATCH (a)-[r]->(b) WHERE r.recordId=$rid AND a.recordId=$rid AND b.recordId=$rid "
            "AND any(l IN labels(a) WHERE l STARTS WITH $prefix) "
            "AND any(l IN labels(b) WHERE l STARTS WITH $prefix) "
            "RETURN r.factId AS id,r.contentHash AS hash",
            rid=self._stored(revision_id),
            prefix=self.settings.namespace + ".",
        )
        actual_edges = {decode_scalar(r["id"]): decode_scalar(r["hash"]) for r in rows}
        expected_edges = {
            e["properties"]["factId"]: e["properties"]["contentHash"] for e in graph["resultEdges"]
        }
        if actual != expected or actual_edges != expected_edges:
            raise ServiceError(
                "图写入回读不一致，知识未发布，请检查 OpenSPG 与 Neo4j 是否指向同一项目存储", 502
            )

    def browse(
        self,
        record_ids: list[str],
        keyword="",
        entity_type="",
        relation_type="",
        region="",
        focus="",
        service="",
        limit=80,
        offset=0,
    ):
        params = dict(
            records=[self._stored(rid) for rid in record_ids],
            prefix=self.settings.namespace + ".",
            keyword=self._fragment(keyword),
            kind=self._stored(entity_type) if entity_type else "",
            rel=self._stored(relation_type) if relation_type else "",
            region=self._stored(region) if region else "",
            focus=focus,
            service=self._fragment(service),
            service_kind=self._stored("ServiceItem"),
            limit=limit + 1,
            offset=offset,
        )
        rows = self._read(
            """MATCH (n)
            WHERE n.recordId IN $records AND any(l IN labels(n) WHERE l STARTS WITH $prefix)
              AND ($kind='' OR n.entityType=$kind) AND ($region='' OR n.region=$region)
              AND ($keyword='' OR n.name CONTAINS $keyword)
              AND ($focus='' OR n.id=$focus OR EXISTS { MATCH (f)-[]-(n) WHERE f.id=$focus AND f.recordId=n.recordId })
              AND ($service='' OR (n.entityType=$service_kind AND n.name CONTAINS $service)
                   OR EXISTS { MATCH (s)-[]-(n) WHERE s.entityType=$service_kind AND s.name CONTAINS $service AND s.recordId=n.recordId })
              AND ($rel='' OR EXISTS { MATCH (n)-[r]-() WHERE r.relationType=$rel AND r.recordId=n.recordId })
            RETURN properties(n) AS node ORDER BY n.id SKIP $offset LIMIT $limit""",
            **params,
        )
        truncated = len(rows) > limit
        nodes = [unpack_node(row["node"]) for row in rows[:limit]]
        ids = [node["id"] for node in nodes]
        edges = self._read(
            """MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids
            AND r.recordId IN $records AND a.recordId=r.recordId AND b.recordId=r.recordId
            AND ($rel='' OR r.relationType=$rel)
            RETURN a.id AS source,b.id AS target,r.relationType AS type,properties(r) AS properties
            ORDER BY r.factId LIMIT 501""",
            ids=ids,
            records=params["records"],
            rel=params["rel"],
        )
        for edge in edges:
            edge["type"] = decode_scalar(edge["type"])
            edge["properties"] = unpack_node(edge["properties"])
        return {
            "nodes": nodes,
            "edges": edges[:500],
            "has_more": truncated or len(edges) > 500,
            "offset": offset,
            "source": "openspg-neo4j",
        }

    def services(self, record_ids, question, region="", service=""):
        label = f"`{self.settings.namespace}.ServiceItem`"
        name = (
            "substring(n.name,1,size(n.name)-2)"
            if self.settings.openspg_scalar_encoding == "json"
            else "n.name"
        )
        rows = self._read(
            f"""MATCH (n:{label}) WHERE n.recordId IN $records
            AND ($region='' OR n.region=$region)
            AND (($service<>'' AND ({name}=$service OR n.aliases CONTAINS $service)) OR
                 ($service='' AND ($question CONTAINS {name} OR n.name CONTAINS $question OR n.aliases <> $empty_aliases)))
            RETURN properties(n) AS node ORDER BY n.name,n.recordId LIMIT 101""",
            records=[self._stored(rid) for rid in record_ids],
            question=self._fragment(question),
            region=self._stored(region) if region else "",
            service=self._fragment(service),
            empty_aliases=self._stored("[]"),
        )
        candidates = []
        for row in rows:
            node = unpack_node(row["node"])
            terms = [node["name"], *node["aliases"]]
            if (service and service in terms) or (
                not service and any(t and (t in question or question in t) for t in terms)
            ):
                candidates.append(node)
        return candidates, len(rows) > 100

    def service_candidates(self, record_ids):
        """名称精确匹配失败时，读取有上限的真实图事项供候选排序。"""
        label = f"`{self.settings.namespace}.ServiceItem`"
        rows = self._read(
            f"MATCH (n:{label}) WHERE n.recordId IN $records "
            "RETURN properties(n) AS node ORDER BY n.name,n.recordId LIMIT 501",
            records=[self._stored(rid) for rid in record_ids],
        )
        return [unpack_node(row["node"]) for row in rows[:500]], len(rows) > 500

    def facts(self, record_ids, service_ids):
        rows = self._read(
            """MATCH (s)-[r]->(t) WHERE s.id IN $ids AND s.recordId IN $records
            AND r.recordId=s.recordId AND t.recordId=s.recordId AND r.relationType<>$evidence_relation
            RETURN properties(s) AS source,r.relationType AS type,properties(r) AS relation,properties(t) AS target
            ORDER BY s.id,r.relationType,t.name LIMIT 501""",
            ids=service_ids,
            records=[self._stored(rid) for rid in record_ids],
            evidence_relation=self._stored("SUPPORTED_BY"),
        )
        return [
            {
                "source": unpack_node(r["source"]),
                "type": decode_scalar(r["type"]),
                "relation": unpack_node(r["relation"]),
                "target": unpack_node(r["target"]),
            }
            for r in rows[:500]
        ], len(rows) > 500
