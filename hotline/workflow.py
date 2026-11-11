import threading
import hashlib
from pathlib import Path

from hotline.models import Chunk, Correction, Extraction, Review
from hotline.ocr import LocalOCR
from hotline.parsing import Parsed, parse_document
from hotline.extraction import Extractor
from hotline.directory import parse_directory, extract_directory
from hotline.schema import DomainSchema
from hotline.settings import ServiceError, Settings, User
from hotline.store import Store, dumps
from hotline.semantics import quality_report
from hotline.fact_review import frozen, manual_baseline, reset_review


class Workflow:
    def __init__(self, settings: Settings, store: Store, extractor, graph, schema: DomainSchema):
        self.settings, self.store, self.extractor, self.graph, self.schema = (
            settings,
            store,
            extractor,
            graph,
            schema,
        )
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.ocr = LocalOCR(settings)
        # 工作进程启动时固定指纹，开发期间修改磁盘文件不能冒充已加载的新代码。
        self.module_hashes = {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in (
                "parsing.py",
                "directory.py",
                "extraction.py",
                "extraction_evidence.py",
                "pdf_tables.py",
                "normalization.py",
                "schema.py",
            )
        }

    def start(self):
        self.store.recover()
        self.thread = threading.Thread(target=self.run, name="hotline-worker", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)

    def run(self):
        while not self.stop.is_set():
            record = self.store.claim()
            if record:
                self.process(record)
            else:
                self.wake.wait(2)
                self.wake.clear()

    def process(self, record):
        rid = record["id"]
        try:
            path = self.store.root / "uploads" / (record["document_id"] + record["extension"])
            data = path.read_bytes()
            saved = (record["parsed_metadata"] or {}).get("_extraction", {})
            parsed = (
                Parsed(
                    record["text"],
                    [Chunk.model_validate(c) for c in record["chunks"]],
                    record["parsed_metadata"],
                )
                if saved
                else (
                    parse_directory(
                        data,
                        record["metadata"].get("directory_mapping"),
                        record["metadata"].get("region", ""),
                        rid,
                        self.settings.max_text_chars,
                        self.settings.max_chunks,
                    )
                    if record["extension"] == ".csv"
                    else parse_document(
                        data,
                        record["extension"],
                        rid,
                        self.settings.max_text_chars,
                        self.settings.max_chunks,
                        ocr=self.ocr,
                        ocr_mode=record["metadata"].get("ocr_mode", "auto"),
                        progress=lambda done, total: self.store.progress(rid, done, total),
                    )
                )
            )
            if not saved:
                self.store.save_parse(rid, parsed)
            with self.store.connect() as db:
                profile = {
                    "schema_sha256": hashlib.sha256(dumps(self.schema.data).encode()).hexdigest(),
                    "modules": self.module_hashes,
                    "model": self.settings.llm_model,
                    "backend": self.settings.llm_backend,
                    "source_sha256": record["sha256"],
                    "ocr_mode": record["metadata"].get("ocr_mode", "auto"),
                }
                self.store.audit(
                    db, rid, "system", "BUILD_PROFILE", "构建输入与代码指纹（不包含密钥）", profile
                )
                self.store.audit(
                    db,
                    rid,
                    "system",
                    "MODEL_REQUEST",
                    f"backend={self.settings.llm_backend}; model={self.settings.llm_model}",
                )
                db.execute("UPDATE revisions SET model=? WHERE id=?", (self.settings.llm_model, rid))
            options = {}
            if isinstance(self.extractor, Extractor):
                options = {
                    "completed": saved,
                    "baseline": (record["parsed_metadata"] or {}).get("_manual_baseline"),
                    "checkpoint": lambda cid, outcome, value: self.store.checkpoint(rid, cid, outcome, value),
                }
            extraction = (
                extract_directory(
                    parsed,
                    self.extractor,
                    self.schema,
                    record["metadata"],
                    rid,
                    lambda done, total: self.store.progress(rid, done, total),
                )
                if record["extension"] == ".csv"
                else self.extractor.extract(
                    parsed.chunks,
                    record["metadata"],
                    rid,
                    lambda done, total: self.store.progress(rid, done, total),
                    **options,
                )
            )
            self.store.finish(rid, extraction, self.settings.llm_model)
        except ServiceError as exc:
            self.store.fail(rid, str(exc))
        except Exception:
            self.store.fail(rid, "处理异常，请检查文档格式与服务配置；未发布任何知识")

    def check_version(self, record, expected):
        if record["version"] != expected:
            raise ServiceError("记录已被其他操作更新，请刷新后重试", 409)
        if record["task_status"] != "READY":
            raise ServiceError("任务尚未成功完成，不能审核或修改", 409)

    def correct(self, rid: str, correction: Correction, user: User):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.store.get_revision(rid, db)
            if record["version"] != correction.expected_version:
                raise ServiceError("记录已被其他操作更新，请刷新后重试", 409)
            if record["task_status"] not in ("READY", "PARTIAL", "FAILED") or not record["chunks"]:
                raise ServiceError("请等待解析完成后修正知识", 409)
            if not correction.note.strip():
                raise ServiceError("请填写具体修改依据", 409)
            if frozen(record):
                raise ServiceError("已有不可变发布快照，请新建修订草稿", 409)
            if record["knowledge_status"] not in ("DRAFT", "REJECTED", "APPROVED"):
                raise ServiceError("已发布、停用或发布中的版本不可覆盖，请重新处理创建新版本", 409)
            if db.execute("SELECT 1 FROM audit WHERE revision_id=? AND action='PUBLISH'", (rid,)).fetchone():
                raise ServiceError("该版本已尝试图发布，可能存在部分写入；请新建版本修正，保留原始记录", 409)
            extraction = self.schema.validate(
                correction.extraction, [Chunk.model_validate(c) for c in record["chunks"]]
            )
            metadata = record["parsed_metadata"] or {}
            reset_review(metadata, extraction.model_dump())
            manual_baseline(record, extraction.model_dump(), metadata)
            db.execute(
                "UPDATE revisions SET extraction=?,parsed_metadata=?,knowledge_status='DRAFT',version=version+1 WHERE id=?",
                (dumps(extraction.model_dump()), dumps(metadata), rid),
            )
            self.store.audit(db, rid, user.name, "CORRECT", correction.note, extraction.model_dump())
        return self.store.get_revision(rid)

    def review(self, rid: str, request: Review, user: User):
        transitions = {
            "approve": ({"DRAFT"}, "APPROVED"),
            "reject": ({"DRAFT", "APPROVED"}, "REJECTED"),
            "publish": ({"APPROVED"}, "PUBLISHING"),
            "retire": ({"PUBLISHED"}, "RETIRED"),
        }
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self.store.get_revision(rid, db)
            if (record["parsed_metadata"] or {}).get("_fact_review") is not None:
                raise ServiceError("该草稿使用事实组审核，请逐组审核发布", 409)
            if (record["parsed_metadata"] or {}).get("_group_publication") and request.action not in (
                "retire",
            ):
                raise ServiceError("请从原草稿的事实组执行发布或重试", 409)
            self.check_version(record, request.expected_version)
            before, after = transitions[request.action]
            if record["knowledge_status"] not in before:
                raise ServiceError("当前知识状态不允许此操作", 409)
            if request.action == "approve":
                self.schema.validate(
                    Extraction.model_validate(record["extraction"]),
                    [Chunk.model_validate(c) for c in record["chunks"]],
                )
                report = quality_report(record["extraction"])
                issue_ids = {issue["id"] for issue in report["issues"]}
                if request.issue_resolutions:
                    if set(request.issue_resolutions) != issue_ids:
                        raise ServiceError("质检问题处置不完整或已过期，请刷新后逐项核对", 409)
                    if any(
                        r.status == "unresolved" or not r.note.strip()
                        for r in request.issue_resolutions.values()
                    ):
                        raise ServiceError(
                            "仍有待确认问题或缺少处置理由，不能审核通过；请修正知识或核对证据", 409
                        )
                if report["requires_acknowledgement"] and not {
                    issue["id"] for issue in report["issues"]
                }.issubset(set(request.acknowledged_issues) | set(request.issue_resolutions)):
                    raise ServiceError("请逐项核对语义质检提示，确认后再审核通过", 409)
            db.execute(
                "UPDATE revisions SET knowledge_status=?,version=version+1,error=NULL WHERE id=?",
                (after, rid),
            )
            self.store.audit(
                db,
                rid,
                user.name,
                request.action.upper(),
                request.note,
                {
                    "acknowledged_issues": request.acknowledged_issues,
                    "issue_resolutions": {
                        key: value.model_dump() for key, value in request.issue_resolutions.items()
                    },
                    "resolution_mode": "per_issue" if request.issue_resolutions else "legacy_acknowledgement",
                },
            )
        if request.action == "publish":
            try:
                self.graph.publish(record)
            except Exception as exc:
                message = str(exc) if isinstance(exc, ServiceError) else "图发布失败，请检查服务配置"
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE revisions SET knowledge_status='APPROVED',error=? WHERE id=?", (message, rid)
                    )
                    self.store.audit(db, rid, user.name, "PUBLISH_FAILED", message)
                raise ServiceError(message, 502) from exc
            with self.store.connect() as db:
                db.execute("UPDATE revisions SET knowledge_status='PUBLISHED' WHERE id=?", (rid,))
                self.store.audit(db, rid, user.name, "PUBLISHED", "图写入与回读校验通过，加入客服查询范围")
        return self.store.get_revision(rid)
