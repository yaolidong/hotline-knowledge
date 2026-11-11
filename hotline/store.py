"""SQLite 仅负责原始证据、任务和审核状态，图谱查询不从此处伪造。"""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from hotline.models import ImportMetadata, now
from hotline.settings import ServiceError


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "uploads").mkdir(exist_ok=True)
        self.path = self.root / "hotline.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL,
                    filename TEXT NOT NULL, extension TEXT NOT NULL, size INTEGER NOT NULL,
                    metadata TEXT NOT NULL, uploaded_by TEXT NOT NULL, uploaded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                    number INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    task_status TEXT NOT NULL, knowledge_status TEXT NOT NULL DEFAULT 'DRAFT',
                    error TEXT, progress INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                    text TEXT, chunks TEXT, parsed_metadata TEXT, extraction TEXT,
                    model TEXT, extracted_at TEXT, created_at TEXT NOT NULL,
                    UNIQUE(document_id,number)
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, revision_id TEXT, actor TEXT NOT NULL,
                    action TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
                    snapshot TEXT
                );
                CREATE INDEX IF NOT EXISTS revision_state ON revisions(knowledge_status,task_status);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit(self, db, revision_id, actor, action, note="", snapshot=None):
        db.execute(
            "INSERT INTO audit(revision_id,actor,action,note,created_at,snapshot) VALUES(?,?,?,?,?,?)",
            (revision_id, actor, action, note, now(), dumps(snapshot) if snapshot else None),
        )

    def ingest(self, data: bytes, filename: str, extension: str, metadata: ImportMetadata, actor: str):
        sha = hashlib.sha256(data).hexdigest()
        # 默认 OCR 策略不改变历史指纹，升级后同一文件仍能去重。
        meta = metadata.model_dump(exclude={"ocr_mode"} if metadata.ocr_mode == "auto" else set())
        if metadata.directory_mapping is None:
            meta.pop("directory_mapping", None)
        fingerprint = hashlib.sha256((sha + dumps(meta)).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute("SELECT id FROM documents WHERE fingerprint=?", (fingerprint,)).fetchone()
            if duplicate:
                row = db.execute(
                    "SELECT id FROM revisions WHERE document_id=? "
                    "AND json_extract(COALESCE(parsed_metadata,'{}'),'$._group_publication') IS NULL "
                    "ORDER BY number DESC LIMIT 1",
                    (duplicate["id"],),
                ).fetchone()
                return {"document_id": duplicate["id"], "revision_id": row["id"], "duplicate": True}
            document_id, revision_id = uuid.uuid4().hex, uuid.uuid4().hex
            path = self.root / "uploads" / (document_id + extension)
            with path.open("xb") as output:
                output.write(data)
            db.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    document_id,
                    fingerprint,
                    sha,
                    filename,
                    extension,
                    len(data),
                    dumps(meta),
                    actor,
                    now(),
                ),
            )
            db.execute(
                "INSERT INTO revisions(id,document_id,number,task_status,created_at) VALUES(?,?,1,'QUEUED',?)",
                (revision_id, document_id, now()),
            )
            self.audit(db, revision_id, actor, "IMPORT")
            return {"document_id": document_id, "revision_id": revision_id, "duplicate": False}

    def reprocess(self, revision_id: str, actor: str):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.get_revision(revision_id, db)
            active = db.execute(
                "SELECT id FROM revisions WHERE document_id=? AND task_status IN ('QUEUED','RUNNING')",
                (row["document_id"],),
            ).fetchone()
            if active:
                raise ServiceError("该文档已有待处理任务", 409)
            number = db.execute(
                "SELECT MAX(number)+1 FROM revisions WHERE document_id=?", (row["document_id"],)
            ).fetchone()[0]
            new_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO revisions(id,document_id,number,task_status,created_at) VALUES(?,?,?,'QUEUED',?)",
                (new_id, row["document_id"], number, now()),
            )
            self.audit(db, new_id, actor, "REPROCESS", "保留历史版本")
            return {"revision_id": new_id, "document_id": row["document_id"]}

    def get_revision(self, revision_id, db=None):
        if db is None:
            with self.connect() as db:
                return self.get_revision(revision_id, db)
        row = db.execute(
            """SELECT r.*,d.filename,d.extension,d.sha256,d.size,d.metadata,d.uploaded_at,d.uploaded_by
                            FROM revisions r JOIN documents d ON d.id=r.document_id WHERE r.id=?""",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise ServiceError("记录不存在", 404)
        result = dict(row)
        for key in ("metadata", "chunks", "parsed_metadata", "extraction"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def list_revisions(self, limit=50, offset=0):
        return self.revision_page(limit, offset)["items"]

    def revision_page(self, limit=50, offset=0, search="", state="", space=None):
        clauses, params = [], []
        if space:
            clauses.append("COALESCE(json_extract(d.metadata,'$.synthetic'),0)=?")
            params.append(int(space == "test"))
        if search:
            clauses.append("(instr(d.filename, ?) > 0 OR instr(d.metadata, ?) > 0)")
            params.extend([search, search])
        if state == "RUNNING":
            clauses.append("r.task_status IN ('QUEUED','RUNNING')")
        elif state in ("FAILED", "PARTIAL", "READY", "QUEUED"):
            clauses.append("r.task_status=?")
            params.append(state)
        elif state:
            clauses.append("r.knowledge_status=? AND r.task_status='READY'")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        joined = " FROM revisions r JOIN documents d ON d.id=r.document_id"
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*)" + joined + where, params).fetchone()[0]
            rows = db.execute(
                """SELECT r.id,r.document_id,r.number,r.version,r.task_status,r.knowledge_status,
                r.error,r.progress,r.total,r.created_at,d.filename,d.metadata,d.extension,
                json_extract(r.parsed_metadata,'$._group_publication.title') AS group_title,
                CASE WHEN r.text IS NULL THEN 'parse' ELSE 'extract' END AS stage"""
                + joined
                + where
                + " ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            return {
                "items": [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows],
                "total": total,
            }

    def recover(self):
        with self.connect() as db:
            # 中断的提取可以重试；中断的发布没有进入白名单，必须显式重新发布。
            db.execute("UPDATE revisions SET task_status='QUEUED' WHERE task_status='RUNNING'")
            rows = db.execute("SELECT id FROM revisions WHERE knowledge_status='PUBLISHING'").fetchall()
            for row in rows:
                db.execute(
                    "UPDATE revisions SET knowledge_status='APPROVED',error=? WHERE id=?",
                    ("上次发布中断，请重新发布并核验图数据", row["id"]),
                )
                self.audit(db, row["id"], "system", "PUBLISH_INTERRUPTED")

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM revisions WHERE task_status='QUEUED' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE revisions SET task_status='RUNNING',error=NULL WHERE id=?", (row["id"],))
            return self.get_revision(row["id"], db)

    def save_parse(self, revision_id, parsed):
        with self.connect() as db:
            db.execute(
                "UPDATE revisions SET text=?,chunks=?,parsed_metadata=?,progress=0,total=? WHERE id=?",
                (
                    parsed.text,
                    dumps([c.model_dump() for c in parsed.chunks]),
                    dumps(parsed.metadata),
                    len(parsed.chunks),
                    revision_id,
                ),
            )

    def progress(self, revision_id, done, total):
        with self.connect() as db:
            db.execute("UPDATE revisions SET progress=?,total=? WHERE id=?", (done, total, revision_id))

    def checkpoint(self, revision_id, chunk_id, outcome, extraction):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.get_revision(revision_id, db)
            metadata = record["parsed_metadata"] or {}
            metadata.setdefault("_extraction", {})[chunk_id] = outcome
            db.execute(
                "UPDATE revisions SET parsed_metadata=?,extraction=? WHERE id=?",
                (
                    dumps(metadata),
                    dumps(extraction.model_dump()) if extraction.entities else None,
                    revision_id,
                ),
            )
            self.audit(
                db,
                revision_id,
                "system",
                "CHUNK_EXTRACTED",
                outcome["error"] or outcome["status"],
                {
                    "chunk_id": chunk_id,
                    "status": outcome["status"],
                    "attempts": outcome["attempts"],
                    "candidate": outcome.get("candidate"),
                },
            )

    def retry_extraction(self, revision_id, expected_version, actor):
        from hotline.fact_review import frozen

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.get_revision(revision_id, db)
            if record["version"] != expected_version:
                raise ServiceError("记录已被其他操作更新，请刷新后重试", 409)
            if frozen(record):
                raise ServiceError("已有不可变发布快照，请新建修订草稿后重试", 409)
            if (
                record["task_status"] not in ("PARTIAL", "FAILED")
                or record["knowledge_status"] != "DRAFT"
                or not (record["parsed_metadata"] or {}).get("_extraction")
            ):
                raise ServiceError("此版本没有可恢复的分块抽取记录，请重新处理创建新版本", 409)
            if db.execute(
                "SELECT 1 FROM revisions WHERE document_id=? AND task_status IN ('QUEUED','RUNNING')",
                (record["document_id"],),
            ).fetchone():
                raise ServiceError("该文档已有待处理任务", 409)
            metadata = record["parsed_metadata"] or {}
            if "_fact_review" in metadata:
                metadata["_fact_review"] = {"items": {}, "groups": {}}
            db.execute(
                "UPDATE revisions SET task_status='QUEUED',error=NULL,version=version+1,parsed_metadata=? WHERE id=?",
                (dumps(metadata), revision_id),
            )
            self.audit(db, revision_id, actor, "RETRY_EXTRACTION", "复用已解析原文和通过校验的块")
        return {"revision_id": revision_id}

    def finish(self, revision_id, extraction, model):
        with self.connect() as db:
            db.execute(
                "UPDATE revisions SET task_status='READY',error=NULL,extraction=?,model=?,extracted_at=? WHERE id=?",
                (dumps(extraction.model_dump()), model, now(), revision_id),
            )
            self.audit(
                db, revision_id, "system", "EXTRACTED", "证据校验通过，仍需人工审核", extraction.model_dump()
            )

    def fail(self, revision_id, message):
        with self.connect() as db:
            record = self.get_revision(revision_id, db)
            state = "PARTIAL" if (record["extraction"] or {}).get("entities") else "FAILED"
            db.execute("UPDATE revisions SET task_status=?,error=? WHERE id=?", (state, message, revision_id))
            self.audit(db, revision_id, "system", state, message)

    def published_sources(self, space=None):
        """仅返回已发布来源的检索上下文，不从 SQLite 构造图事实。"""
        with self.connect() as db:
            rows = db.execute(
                "SELECT r.id,d.filename,json_extract(d.metadata,'$.title') AS title,"
                "json_extract(d.metadata,'$.region') AS region "
                "FROM revisions r JOIN documents d ON d.id=r.document_id "
                "WHERE r.knowledge_status='PUBLISHED' "
                + ("AND COALESCE(json_extract(d.metadata,'$.synthetic'),0)=? " if space else "")
                + "ORDER BY r.id",
                (int(space == "test"),) if space else (),
            )
            return {row["id"]: dict(row) for row in rows}

    def visible_ids(self, published_only=True, space=None):
        with self.connect() as db:
            query = "SELECT r.id FROM revisions r JOIN documents d ON d.id=r.document_id"
            clauses, params = [], []
            if published_only:
                clauses.append("r.knowledge_status='PUBLISHED'")
            if space:
                clauses.append("COALESCE(json_extract(d.metadata,'$.synthetic'),0)=?")
                params.append(int(space == "test"))
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            return [r[0] for r in db.execute(query, params)]

    def history(self, revision_id):
        self.get_revision(revision_id)
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM audit WHERE revision_id=? ORDER BY id", (revision_id,))
            ]
