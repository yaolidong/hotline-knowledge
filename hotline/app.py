import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from hotline.extraction import Extractor, ModelClient
from hotline.documents import IMAGE_FORMATS, open_image, source_info, render_source
from hotline.graph import GraphStore
from hotline.models import Correction, ImportMetadata, Question, Review, TextImport, RetryExtraction
from hotline.query import QueryService
from hotline.qa import QAService
from hotline.schema import DomainSchema
from hotline.settings import ServiceError, Settings, User
from hotline.store import Store
from hotline.workflow import Workflow
from hotline.semantics import quality_report
from hotline.evidence import locate_evidence
from hotline.identity import IdentityDecision, IdentityService
from hotline.operations import Feedback, FeedbackReview, QueryJournal
from hotline.workbench import ServiceWorkbench
from hotline.changes import ChangeService
from hotline.directory import preview as preview_directory, parse_directory
from hotline.reasoning import ReasoningService, PathRequest, ConditionRequest
from hotline.fact_review import FactReviewService, ReviewCommand, ItemDecision, GroupDecision, ManualBlock
from hotline.rate_limit import TokenBucketLimiter
from hotline.local_llm import is_local_preset_url, probe_local_llm

STATIC = Path(__file__).parent / "static"
RATE_LIMITED_PREFIXES = ("/api/query", "/api/qa", "/api/import", "/api/feedback")


def create_app(settings: Settings | None = None, *, extractor=None, graph=None, qa_model=None):
    settings = settings or Settings()
    schema = DomainSchema()
    store = Store(settings.data_dir)
    graph = graph or GraphStore(settings)
    model = ModelClient(settings)
    extractor = extractor or Extractor(model, schema, settings.extraction_validation_retries)
    workflow = Workflow(settings, store, extractor, graph, schema)
    query = QueryService(store, graph, schema, settings.default_knowledge_space)
    qa = QAService(store, query, qa_model or model, settings)
    identity = IdentityService(store)
    journal = QueryJournal(store)
    workbench = ServiceWorkbench(store, graph, query, qa)
    changes = ChangeService(store, graph)
    reasoning = ReasoningService(store, graph, query)
    facts = FactReviewService(store, schema, graph)
    limiter = TokenBucketLimiter(settings.rate_limit_per_minute, settings.rate_limit_burst)

    @asynccontextmanager
    async def lifespan(app):
        if settings.worker_enabled:
            workflow.start()
        yield
        workflow.close()
        graph.close()

    app = FastAPI(title="政务热线知识工作台", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.workflow, app.state.graph = store, workflow, graph
    app.state.qa = qa
    bearer = HTTPBearer(auto_error=False)

    def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> User:
        if credentials and credentials.scheme.lower() == "bearer":
            for token, user in settings.users.items():
                if secrets.compare_digest(credentials.credentials, token):
                    return user
        raise HTTPException(401, "访问令牌无效或尚未配置", headers={"WWW-Authenticate": "Bearer"})

    def roles(*allowed):
        def check(user: User = Depends(authenticate)):
            if user.role not in allowed:
                raise HTTPException(403, "当前角色无权执行此操作")
            return user

        return check

    maintain = roles("admin", "maintainer", "reviewer")
    modify = roles("admin", "maintainer")
    reviewer = roles("admin", "reviewer")

    def knowledge_space(space: Literal["production", "test"] | None = None):
        return space or settings.default_knowledge_space

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # 不在错误响应反射上传正文或令牌。
        return JSONResponse(
            {"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]},
            status_code=422,
        )

    @app.middleware("http")
    async def limits_and_headers(request: Request, call_next):
        if (
            settings.rate_limit_enabled
            and request.method == "POST"
            and any(request.url.path.startswith(prefix) for prefix in RATE_LIMITED_PREFIXES)
        ):
            auth = request.headers.get("authorization", "")
            key = auth[-32:] if auth else request.client.host if request.client else "anonymous"
            if not limiter.allow(key):
                return JSONResponse({"detail": "请求过于频繁，请稍后再试"}, status_code=429)
        if request.method in ("POST", "PUT", "PATCH"):
            maximum = settings.max_upload_mb * 1024 * 1024 + 1024 * 1024
            length = request.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > maximum):
                return JSONResponse({"detail": "请求体大小超限"}, status_code=413)
            body, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > maximum:
                    return JSONResponse({"detail": "请求体大小超限"}, status_code=413)
                body.append(chunk)
            request._body = b"".join(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
            )
        return response

    @app.get("/health")
    def health():
        graph_ok = False
        try:
            graph.health()
            graph_ok = True
        except Exception:
            graph_ok = False
        model_ok = bool(settings.llm_base_url and settings.llm_model)
        return {
            "status": "ok" if model_ok else "degraded",
            "service": "hotline-knowledge",
            "checks": {
                "model_configured": model_ok,
                "graph_reachable": graph_ok,
                "project_configured": settings.openspg_project_id > 0,
                "worker_enabled": settings.worker_enabled,
            },
        }

    @app.get("/api/me")
    def me(user=Depends(authenticate)):
        return {**user.model_dump(), "default_space": settings.default_knowledge_space}

    @app.get("/api/status")
    def status(user=Depends(maintain)):
        graph_error = None
        try:
            graph.health()
        except ServiceError as exc:
            graph_error = str(exc)
        local_llm = {"status": "未配置"}
        if settings.llm_base_url and settings.llm_model:
            if is_local_preset_url(settings.llm_base_url):
                local_llm = probe_local_llm(
                    base_url=settings.llm_base_url,
                    model=settings.llm_model,
                    api_key=settings.llm_api_key.get_secret_value(),
                    timeout=min(float(settings.llm_timeout), 30.0),
                    trust_env=settings.http_trust_env,
                )
            else:
                local_llm = {"status": "external", "detail": "已配置非本机预设模型地址，未在此探测"}
        return {
            "model_configured": bool(settings.llm_base_url and settings.llm_model),
            "model": settings.llm_model,
            "llm_backend": settings.llm_backend,
            "graph_ready": graph_error is None,
            "graph_error": graph_error,
            "project_configured": settings.openspg_project_id > 0,
            "worker_enabled": settings.worker_enabled,
            "ocr": workflow.ocr.status(),
            "max_upload_mb": settings.max_upload_mb,
            "rate_limit": {
                "enabled": settings.rate_limit_enabled,
                "per_minute": settings.rate_limit_per_minute,
                "burst": settings.rate_limit_burst,
            },
            "local_llm": {
                "status": local_llm.get("status"),
                "detail": local_llm.get("detail"),
            },
        }

    @app.get("/api/operations/summary")
    def operation_summary(
        days: int = Query(7, ge=1, le=30), user=Depends(maintain), space=Depends(knowledge_space)
    ):
        return journal.summary(space, days)

    @app.get("/api/operations/audit-export")
    def audit_export(
        since: str | None = None,
        limit: int = Query(200, ge=1, le=1000),
        action: str | None = Query(None, max_length=64),
        user=Depends(maintain),
    ):
        return journal.export_audit(since=since, limit=limit, action=action)

    @app.get("/api/schema")
    def get_schema(user=Depends(authenticate)):
        return schema.data

    @app.post("/api/import/text", status_code=202)
    def import_text(body: TextImport, user=Depends(modify)):
        if body.directory_mapping is not None:
            raise ServiceError("事项目录映射只能用于 CSV 目录导入")
        if not body.text.strip() or len(body.text) > settings.max_text_chars:
            raise ServiceError("文本为空或长度超限")
        metadata = ImportMetadata.model_validate(body.model_dump(exclude={"text"}))
        result = store.ingest(
            body.text.encode("utf-8"), (body.title or "直接输入文本") + ".txt", ".txt", metadata, user.name
        )
        workflow.wake.set()
        return result

    @app.post("/api/import/files", status_code=202)
    async def import_files(
        files: list[UploadFile] = File(...), metadata: str = Form("{}"), user=Depends(modify)
    ):
        if not 1 <= len(files) <= 10:
            raise ServiceError("每批允许上传 1 至 10 个文件")
        try:
            meta = ImportMetadata.model_validate_json(metadata)
        except ValueError as exc:
            raise ServiceError("导入元数据格式不正确") from exc
        if meta.directory_mapping is not None:
            raise ServiceError("请使用专用事项目录入口导入 CSV")
        pending, size = [], 0
        for file in files:
            filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
            extension = Path(filename).suffix.lower()
            if extension not in (".pdf", ".docx", ".txt", *IMAGE_FORMATS):
                raise ServiceError("仅支持 PDF、DOCX、TXT、PNG、JPEG、WebP 文件")
            data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
            await file.close()
            size += len(data)
            if not data or size > settings.max_upload_mb * 1024 * 1024:
                raise ServiceError("文件为空或批次大小超限", 413)
            # 签名校验在创建任务之前，复杂解析由持久化任务完成。
            if extension == ".pdf" and not data.startswith(b"%PDF-"):
                raise ServiceError("PDF 文件签名不正确")
            if extension == ".docx" and not data.startswith(b"PK"):
                raise ServiceError("DOCX 文件签名不正确")
            if extension in IMAGE_FORMATS:
                with open_image(data, extension):
                    pass
            pending.append((data, filename, extension))
        results = [store.ingest(data, name, ext, meta, user.name) for data, name, ext in pending]
        workflow.wake.set()
        return {"items": results}

    async def directory_file(file):
        filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        if Path(filename).suffix.lower() != ".csv":
            raise ServiceError("事项目录入口当前支持 CSV，请将工作表导出为 UTF-8 CSV")
        data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
        await file.close()
        if not data or len(data) > settings.max_upload_mb * 1024 * 1024:
            raise ServiceError("目录文件为空或大小超限", 413)
        return filename, data

    @app.post("/api/import/directory/preview")
    async def directory_preview(file: UploadFile = File(...), user=Depends(modify)):
        _, data = await directory_file(file)
        return preview_directory(data)

    @app.post("/api/import/directory", status_code=202)
    async def directory_import(file: UploadFile = File(...), metadata: str = Form(...), user=Depends(modify)):
        filename, data = await directory_file(file)
        try:
            meta = ImportMetadata.model_validate_json(metadata)
        except ValueError as exc:
            raise ServiceError("目录元数据或字段映射格式不正确") from exc
        parse_directory(
            data, meta.directory_mapping, meta.region, "preview", settings.max_text_chars, settings.max_chunks
        )
        result = store.ingest(data, filename, ".csv", meta, user.name)
        workflow.wake.set()
        return result

    @app.get("/api/revisions")
    def revisions(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        search: str = Query("", max_length=300),
        state: str = Query(
            "", pattern="^(|QUEUED|RUNNING|READY|PARTIAL|FAILED|DRAFT|APPROVED|PUBLISHED|REJECTED|RETIRED)$"
        ),
        user=Depends(maintain),
        space=Depends(knowledge_space),
    ):
        return store.revision_page(limit, offset, search, state, space)

    @app.get("/api/revisions/{rid}")
    def revision(rid: str, user=Depends(maintain)):
        record = store.get_revision(rid)
        return {**record, "quality": quality_report(record["extraction"]) if record["extraction"] else None}

    @app.post("/api/revisions/{rid}/reprocess", status_code=202)
    def reprocess(rid: str, user=Depends(modify)):
        result = store.reprocess(rid, user.name)
        workflow.wake.set()
        return result

    @app.put("/api/revisions/{rid}/extraction")
    def correction(rid: str, body: Correction, user=Depends(modify)):
        return workflow.correct(rid, body, user)

    @app.post("/api/revisions/{rid}/retry-extraction", status_code=202)
    def retry_extraction(rid: str, body: RetryExtraction, user=Depends(modify)):
        result = store.retry_extraction(rid, body.expected_version, user.name)
        workflow.wake.set()
        return result

    @app.post("/api/revisions/{rid}/review")
    def review(rid: str, body: Review, user=Depends(reviewer)):
        return workflow.review(rid, body, user)

    @app.get("/api/revisions/{rid}/fact-review")
    def fact_review(rid: str, user=Depends(maintain)):
        record = store.get_revision(rid)
        result = facts.detail(record)
        for group in result["groups"]:
            snapshot = group["decision"].get("publication_revision_id")
            if snapshot:
                group["publication_status"] = store.get_revision(snapshot)["knowledge_status"]
        return {**result, "version": record["version"]}

    @app.post("/api/revisions/{rid}/facts/{key}/decision")
    def fact_decision(rid: str, key: str, body: ItemDecision, user=Depends(reviewer)):
        return facts.decide_item(rid, key, body, user)

    @app.post("/api/revisions/{rid}/groups/{gid}/review")
    def group_review(rid: str, gid: str, body: GroupDecision, user=Depends(reviewer)):
        return facts.decide_group(rid, gid, body, user)

    @app.post("/api/revisions/{rid}/groups/{gid}/publish")
    def group_publish(rid: str, gid: str, body: ReviewCommand, user=Depends(reviewer)):
        return facts.publish_group(rid, gid, body, user)

    @app.put("/api/revisions/{rid}/chunks/{cid}/manual")
    def manual_block(rid: str, cid: str, body: ManualBlock, user=Depends(modify)):
        return facts.repair_block(rid, cid, body, user)

    @app.post("/api/revisions/{rid}/review-copy", status_code=201)
    def review_copy(rid: str, body: ReviewCommand, user=Depends(modify)):
        return facts.copy_draft(rid, body, user)

    @app.get("/api/revisions/{rid}/history")
    def history(rid: str, user=Depends(maintain)):
        return {"items": store.history(rid)}

    @app.get("/api/revisions/{rid}/changes")
    def revision_changes(rid: str, before: str = Query("", max_length=100), user=Depends(maintain)):
        return changes.compare(rid, before)

    @app.post("/api/reasoning/paths")
    def fact_paths(body: PathRequest, user=Depends(authenticate)):
        return reasoning.paths(body)

    @app.post("/api/reasoning/conditions")
    def condition_check(body: ConditionRequest, user=Depends(authenticate)):
        return reasoning.conditions(body)

    @app.get("/api/revisions/{rid}/identity-candidates")
    def identity_candidates(rid: str, entity_id: str = Query(max_length=100), user=Depends(maintain)):
        return identity.candidates(rid, entity_id)

    @app.post("/api/identity/decisions")
    def identity_decision(body: IdentityDecision, user=Depends(reviewer)):
        return identity.decide(body, user)

    def source_access(rid, user, space):
        record = store.get_revision(rid)
        if user.role == "agent" and record["knowledge_status"] != "PUBLISHED":
            raise HTTPException(403, "客服只能查看已发布知识的来源")
        if user.role == "agent" and bool(record["metadata"].get("synthetic")) != (space == "test"):
            raise HTTPException(403, "来源不在当前资料空间，请明确切换后查看")
        return record

    @app.get("/api/evidence/{rid}/{chunk_id}")
    def evidence(rid: str, chunk_id: str, user=Depends(authenticate), space=Depends(knowledge_space)):
        record = source_access(rid, user, space)
        chunk = next((c for c in record["chunks"] or [] if c["id"] == chunk_id), None)
        if chunk is None:
            raise ServiceError("原文块不存在", 404)
        return {
            "chunk": chunk,
            "metadata": record["metadata"],
            "filename": record["filename"],
            "review_status": record["knowledge_status"],
            "version": record["number"],
        }

    @app.get("/api/source/{rid}")
    def source(rid: str, user=Depends(authenticate), space=Depends(knowledge_space)):
        record = source_access(rid, user, space)
        path = store.root / "uploads" / (record["document_id"] + record["extension"])
        return FileResponse(path, filename=record["filename"], media_type="application/octet-stream")

    @app.get("/api/evidence/{rid}/{chunk_id}/locate")
    def locate(
        rid: str,
        chunk_id: str,
        start: int = Query(ge=0),
        end: int = Query(gt=0),
        user=Depends(authenticate),
        space=Depends(knowledge_space),
    ):
        record = source_access(rid, user, space)
        chunk = next((c for c in record["chunks"] or [] if c["id"] == chunk_id), None)
        if chunk is None:
            raise ServiceError("原文块不存在", 404)
        return locate_evidence(record, chunk, start, end)

    @app.get("/api/source/{rid}/text")
    def source_text(rid: str, user=Depends(authenticate), space=Depends(knowledge_space)):
        record = source_access(rid, user, space)
        return {
            key: record[key]
            for key in (
                "id",
                "filename",
                "metadata",
                "chunks",
                "knowledge_status",
                "number",
                "parsed_metadata",
            )
        }

    @app.get("/api/source/{rid}/info")
    def original_info(rid: str, user=Depends(authenticate), space=Depends(knowledge_space)):
        record = source_access(rid, user, space)
        path = store.root / "uploads" / (record["document_id"] + record["extension"])
        return {
            **source_info(path.read_bytes(), record["extension"]),
            "filename": record["filename"],
            "title": record["metadata"].get("title"),
            "source": record["metadata"].get("source"),
            "revision": record["number"],
            "knowledge_status": record["knowledge_status"],
            "pages": (record["parsed_metadata"] or {}).get("_source", {}).get("pages", []),
        }

    @app.get("/api/source/{rid}/pages/{page_number}")
    def original_page(
        rid: str,
        page_number: int,
        size: int = Query(1800, ge=600, le=3000),
        user=Depends(authenticate),
        space=Depends(knowledge_space),
    ):
        record = source_access(rid, user, space)
        path = store.root / "uploads" / (record["document_id"] + record["extension"])
        content = render_source(path.read_bytes(), record["extension"], page_number, size)
        # 渲染期间被停用的来源也不能继续返回给客服。
        source_access(rid, user, space)
        return Response(content, media_type="image/png")

    @app.get("/api/graph")
    def browse(
        keyword: str = Query("", max_length=300),
        entity_type: str = "",
        relation_type: str = "",
        region: str = Query("", max_length=100),
        focus: str = Query("", max_length=100),
        service: str = Query("", max_length=300),
        revision_id: str = Query("", max_length=100),
        limit: int = Query(60, ge=1, le=150),
        offset: int = Query(0, ge=0),
        user=Depends(authenticate),
        space=Depends(knowledge_space),
    ):
        if entity_type and entity_type not in {*schema.entities, "Chunk", "SourceDocument"}:
            raise ServiceError("实体类型不合法")
        if relation_type and relation_type not in {*schema.relations, "SUPPORTED_BY", "HAS_CHUNK"}:
            raise ServiceError("关系类型不合法")
        visible = store.visible_ids(published_only=user.role == "agent", space=space)
        if revision_id:
            if revision_id not in visible:
                raise ServiceError("该版本不在当前可见图谱范围内", 403)
            visible = [revision_id]
        result = graph.browse(
            visible,
            keyword=keyword,
            entity_type=entity_type,
            relation_type=relation_type,
            region=region,
            focus=focus,
            service=service,
            limit=limit,
            offset=offset,
        )
        statuses = {}
        for node in result["nodes"]:
            rid = node["recordId"]
            if rid not in statuses:
                statuses[rid] = store.get_revision(rid)["knowledge_status"]
            node["reviewStatus"] = statuses[rid]
        if user.role == "agent" and any(status != "PUBLISHED" for status in statuses.values()):
            raise ServiceError("图谱查询期间知识状态已变化，请重试", 409)
        return result

    @app.post("/api/query")
    def ask(body: Question, user=Depends(authenticate)):
        return journal.record(body, query.ask(body), user, "query")

    @app.post("/api/qa")
    def answer_with_evidence(body: Question, user=Depends(authenticate)):
        return journal.record(body, qa.ask(body, actor=user.name), user, "qa")

    @app.get("/api/services")
    def services(
        keyword: str = Query("", max_length=300),
        region: str = Query("", max_length=100),
        limit: int = Query(30, ge=1, le=100),
        offset: int = Query(0, ge=0),
        space=Depends(knowledge_space),
        user=Depends(authenticate),
    ):
        return workbench.services(space, keyword, region, limit, offset)

    @app.post("/api/retrieval/diagnose")
    def diagnose(body: Question, user=Depends(maintain)):
        return workbench.diagnose(body)

    @app.post("/api/feedback", status_code=201)
    def feedback(body: Feedback, user=Depends(authenticate)):
        return journal.submit(body, user)

    @app.get("/api/feedback")
    def feedback_list(
        limit: int = Query(30, ge=1, le=100),
        offset: int = Query(0, ge=0),
        space=Depends(knowledge_space),
        user=Depends(maintain),
    ):
        return journal.items(limit, offset, space)

    @app.post("/api/feedback/{feedback_id}/review")
    def feedback_review(feedback_id: int, body: FeedbackReview, user=Depends(modify)):
        return journal.review(feedback_id, body, user)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
