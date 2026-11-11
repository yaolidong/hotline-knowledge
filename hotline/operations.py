"""查询追踪与业务反馈。复用审计账本，不默认保存来电问题或个人信息。"""

import hashlib
import json
import uuid
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import Field

from hotline.models import StrictModel
from hotline.settings import ServiceError
from hotline.store import dumps


class Feedback(StrictModel):
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    category: Literal["wrong_evidence", "irrelevant", "missing_requirement", "outdated", "helpful"]
    note: str = Field("", max_length=500)
    question: str = Field("", max_length=1000)


class FeedbackReview(StrictModel):
    expected_event_id: int = Field(ge=1)
    status: Literal["RESOLVED", "DISMISSED", "OPEN"]
    note: str = Field(min_length=1, max_length=1000)


class QueryJournal:
    def __init__(self, store):
        self.store = store

    def summary(self, space, days):
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.store.connect() as db:
            states = db.execute(
                "SELECT r.task_status,r.knowledge_status,COUNT(*) AS count FROM revisions r "
                "JOIN documents d ON d.id=r.document_id WHERE COALESCE(json_extract(d.metadata,'$.synthetic'),0)=? "
                "GROUP BY r.task_status,r.knowledge_status",
                (int(space == "test"),),
            ).fetchall()
            traces = db.execute(
                "SELECT snapshot FROM audit WHERE action='QUERY_TRACE' AND created_at>=? "
                "AND json_extract(snapshot,'$.space')=? ORDER BY id DESC LIMIT 1001",
                (since, space),
            ).fetchall()
        values = [json.loads(r["snapshot"]) for r in traces[:1000]]
        performance = []
        for mode in ("query", "qa"):
            samples = [r for r in values if r["mode"] == mode]
            timings = sorted(
                r["latency_ms"]
                for r in samples
                if isinstance(r.get("latency_ms"), (int, float))
                and math.isfinite(r["latency_ms"])
                and r["latency_ms"] >= 0
            )
            performance.append(
                {
                    "mode": mode,
                    "samples": len(samples),
                    "timing_samples": len(timings),
                    "p50_ms": timings[math.ceil(len(timings) * 0.50) - 1] if timings else None,
                    "p95_ms": timings[math.ceil(len(timings) * 0.95) - 1] if timings else None,
                    "statuses": dict(Counter(r["status"] for r in samples)),
                }
            )
        return {
            "space": space,
            "days": days,
            "since": since,
            "revision_states": [dict(row) for row in states],
            "performance": performance,
            "traces_sampled": len(values),
            "truncated": len(traces) > 1000,
            "message": "版本状态为当前存量；耗时来自时间窗内最近最多 1000 条已完成并记录的请求，按 nearest-rank 计算。没有记录的请求异常不在统计中，不能作为压测指标或准确率。",
        }

    def export_audit(self, *, since: str | None = None, limit: int = 200, action: str | None = None):
        limit = max(1, min(int(limit), 1000))
        clauses = ["1=1"]
        params: list = []
        if since:
            clauses.append("created_at>=?")
            params.append(since)
        if action:
            clauses.append("action=?")
            params.append(action)
        params.append(limit)
        sql = (
            "SELECT id,revision_id,actor,action,note,created_at,"
            "CASE WHEN snapshot IS NULL THEN NULL ELSE json_extract(snapshot,'$.trace_id') END AS trace_id "
            f"FROM audit WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?"
        )
        with self.store.connect() as db:
            rows = db.execute(sql, params).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "limit": limit,
            "note": "导出不含完整 snapshot 正文，避免把问答原文或密钥带出；运维排查用 id/trace_id 回查库内审计。",
        }

    def record(self, request, result, user, mode):
        trace_id = uuid.uuid4().hex
        snapshot = {
            "trace_id": trace_id,
            "space": result["space"],
            "mode": mode,
            "question_hash": hashlib.sha256(request.question.encode()).hexdigest(),
            "scope": result.get("scope", {}),
            "as_of": result.get("as_of"),
            "status": result["status"],
            "latency_ms": result.get("latency_ms"),
            "retrieval": result.get("retrieval", {}),
            "generation": result.get("generation", {}),
            "sources": sorted({c["revision_id"] for c in result.get("citations", [])}),
            "result_hash": hashlib.sha256(dumps(result).encode()).hexdigest(),
        }
        with self.store.connect() as db:
            self.store.audit(
                db, None, user.name, "QUERY_TRACE", "查询范围及证据版本，未保存问题正文", snapshot
            )
        return {**result, "trace_id": trace_id}

    def trace(self, trace_id, db):
        row = db.execute(
            "SELECT actor,snapshot,created_at FROM audit WHERE action='QUERY_TRACE' "
            "AND json_extract(snapshot,'$.trace_id')=? ORDER BY id DESC LIMIT 1",
            (trace_id,),
        ).fetchone()
        if row is None:
            raise ServiceError("查询记录不存在", 404)
        return {**json.loads(row["snapshot"]), "actor": row["actor"], "created_at": row["created_at"]}

    def submit(self, body, user):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            trace = self.trace(body.trace_id, db)
            if trace["actor"] != user.name:
                raise ServiceError("只能反馈自己的查询记录", 403)
            duplicate = db.execute(
                "SELECT id FROM audit WHERE action='QUERY_FEEDBACK' AND actor=? "
                "AND json_extract(snapshot,'$.trace_id')=? AND json_extract(snapshot,'$.category')=?",
                (user.name, body.trace_id, body.category),
            ).fetchone()
            if duplicate:
                return {"id": duplicate["id"], "duplicate": True}
            self.store.audit(
                db, None, user.name, "QUERY_FEEDBACK", body.note, body.model_dump(exclude={"note"})
            )
            return {"id": db.execute("SELECT last_insert_rowid()").fetchone()[0], "duplicate": False}

    def latest(self, db, feedback_id):
        row = db.execute(
            "SELECT * FROM audit WHERE action='FEEDBACK_REVIEW' AND json_extract(snapshot,'$.feedback_id')=? "
            "ORDER BY id DESC LIMIT 1",
            (feedback_id,),
        ).fetchone()
        return (
            {**json.loads(row["snapshot"]), "event_id": row["id"], "note": row["note"], "actor": row["actor"]}
            if row
            else {"event_id": feedback_id, "status": "OPEN", "note": ""}
        )

    def items(self, limit, offset, space):
        with self.store.connect() as db:
            join = (
                " FROM audit f JOIN audit t ON t.action='QUERY_TRACE' "
                "AND json_extract(t.snapshot,'$.trace_id')=json_extract(f.snapshot,'$.trace_id') "
                "WHERE f.action='QUERY_FEEDBACK' AND json_extract(t.snapshot,'$.space')=?"
            )
            total = db.execute("SELECT COUNT(*)" + join, (space,)).fetchone()[0]
            rows = db.execute(
                "SELECT f.*" + join + " ORDER BY f.id DESC LIMIT ? OFFSET ?", (space, limit, offset)
            ).fetchall()
            items = []
            for row in rows:
                data = json.loads(row["snapshot"])
                items.append(
                    {
                        **data,
                        "id": row["id"],
                        "note": row["note"],
                        "actor": row["actor"],
                        "created_at": row["created_at"],
                        "trace": self.trace(data["trace_id"], db),
                        "review": self.latest(db, row["id"]),
                    }
                )
            return {"items": items, "total": total}

    def review(self, feedback_id, body, user):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM audit WHERE id=? AND action='QUERY_FEEDBACK'", (feedback_id,)
            ).fetchone():
                raise ServiceError("反馈不存在", 404)
            if self.latest(db, feedback_id)["event_id"] != body.expected_event_id:
                raise ServiceError("反馈已经被处理，请刷新后重试", 409)
            self.store.audit(
                db,
                None,
                user.name,
                "FEEDBACK_REVIEW",
                body.note,
                {"feedback_id": feedback_id, "status": body.status},
            )
            return self.latest(db, feedback_id)
