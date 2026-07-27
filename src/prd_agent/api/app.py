from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from prd_agent.domain.commands import (
    ApproveRevisionPlan,
    ConfirmOutline,
    ConfirmUnit,
    FinalizePrd,
    ReopenPrd,
    ReplyToTask,
    StartTask,
)
from prd_agent.domain.errors import (
    ActiveRunConflict,
    IdempotencyConflict,
    InvalidCommand,
    InvalidTransition,
    ModelOutputError,
    NotFound,
    VersionConflict,
    WorkflowError,
)
from prd_agent.application.default_investigation import (
    DefaultUnitInvestigationContextProvider,
    RepositoryStructureSelector,
)
from prd_agent.application.investigation_read_service import (
    InvestigationReadService,
)
from prd_agent.application.investigation_service import (
    InvestigationApplicationService,
)
from prd_agent.application.repository_evidence_service import (
    RepositoryEvidenceService,
)
from prd_agent.evidence.deterministic_validator import (
    DeterministicEvidenceValidator,
)
from prd_agent.grounding.service import GroundingService
from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode
from prd_agent.investigation.runner import InvestigationRunner
from prd_agent.production.identity import (
    AuthenticationError,
    AuthorizationError,
    LocalPrincipalResolver,
    OidcJwtPrincipalResolver,
    PostgresIdentityDirectory,
)
from prd_agent.production.database import ProductionDatabase
from prd_agent.production.config import secret_or_environment
from prd_agent.production.postgres_dispatch import (
    PostgresProductionControlStore,
)
from prd_agent.quality.service import DocumentQualityService
from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.storage.postgres import PostgresWorkflowRepository
from prd_agent.storage.postgres_evidence import PostgresEvidenceStore
from prd_agent.storage.postgres_investigation import PostgresInvestigationStore
from prd_agent.tools.default_registry import build_repository_tool_registry
from prd_agent.workflow.service import WorkflowService
from prd_agent.workflow.stub_model import HeuristicWorkflowModel

from .mapping import (
    PUBLIC_EVENT_NAMES,
    public_event_payload,
    task_detail,
    task_list,
)
from .models import (
    ApproveRevisionRequest,
    ConfirmOutlineRequest,
    ConfirmUnitRequest,
    ErrorResponse,
    ExecuteExportRequest,
    ExportListResponse,
    ExportPreviewRequest,
    ExportPreviewView,
    ExportRunView,
    FinalizeRequest,
    ReopenRequest,
    RetryRunRequest,
    RepositoryBindingListResponse,
    RepositoryBindingView,
    ReplyRequest,
    StartTaskRequest,
    TaskDetailResponse,
    TaskListResponse,
)


LOCAL_USER = "local-user"


def _error_payload(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    message: str,
    retryable: bool = False,
) -> dict:
    return ErrorResponse(
        title=title,
        status=status,
        error_code=code,
        message=message,
        retryable=retryable,
        correlation_id=request.state.request_id,
    ).model_dump()


def _rollback(repository) -> None:
    connection = getattr(repository, "connection", None)
    if connection is not None:
        connection.rollback()


def _actor(request: Request, *required_scopes: str) -> str:
    principal = request.state.principal
    principal.require(*required_scopes)
    return principal.user_id


def _execute(app: FastAPI, command):
    try:
        result = command()
        app.state.repository.commit()
        return result
    except Exception:
        _rollback(app.state.repository)
        raise


def _detail(app: FastAPI, task_id: str, actor_id: str):
    try:
        snapshot = app.state.workflow.show(task_id, actor_id)
        sequence = app.state.repository.latest_event_sequence(task_id, actor_id)
        records = (
            app.state.investigation_reader.for_task(task_id)
            if app.state.investigation_reader is not None
            else ()
        )
        result = task_detail(snapshot, sequence, records)
        app.state.repository.commit()
        return result
    except Exception:
        _rollback(app.state.repository)
        raise


def _default_agent_core(repository, dsn: str):
    project_root = Path(__file__).resolve().parents[3]
    repository_root = Path(
        os.environ.get("PRD_AGENT_REPOSITORY_ROOT", str(project_root))
    ).resolve()
    repository_prefix = os.environ.get(
        "PRD_AGENT_REPOSITORY_PREFIX", "eval/fixtures/demo-repo"
    )
    repository_id = os.environ.get(
        "PRD_AGENT_REPOSITORY_ID", "demo-repo"
    )
    revision = os.environ.get("PRD_AGENT_REPOSITORY_REVISION", "HEAD")
    reader = GitCliObjectReader(
        RepositoryCatalog(
            [
                RepositoryBinding(
                    repository_id,
                    repository_root,
                    repository_prefix,
                    revision,
                )
            ]
        )
    )
    evidence_store = PostgresEvidenceStore.from_dsn(dsn)
    investigation_store = PostgresInvestigationStore.from_dsn(dsn)
    evidence_service = RepositoryEvidenceService(
        reader,
        build_repository_tool_registry(reader),
        evidence_store,
        DeterministicEvidenceValidator(reader),
    )
    investigation_runner = InvestigationRunner(
        evidence_service,
        investigation_store,
        RepositoryStructureSelector(),
    )
    investigation_application = InvestigationApplicationService(
        investigation_store,
        investigation_runner,
    )
    provider = DefaultUnitInvestigationContextProvider(
        investigation_application,
        evidence_store,
        repository_id=repository_id,
        revision=revision,
    )
    workflow = WorkflowService(
        repository,
        HeuristicWorkflowModel(),
        quality_service=DocumentQualityService(),
        unit_context_provider=provider,
        grounding_service=GroundingService(),
    )
    return (
        workflow,
        InvestigationReadService(investigation_store, evidence_store),
        (evidence_store.connection, investigation_store.connection),
    )


def create_app(
    repository=None,
    workflow_service=None,
    investigation_reader=None,
    export_service=None,
    remote_repository_catalog=None,
    principal_resolver=None,
    production_control_store=None,
    cancellation_signal=None,
) -> FastAPI:
    dsn = (
        secret_or_environment("PRD_AGENT_DATABASE_DSN")
        if os.environ.get("PRD_AGENT_DATABASE_DSN")
        or os.environ.get("PRD_AGENT_DATABASE_DSN_FILE")
        else None
    )
    production_database = None
    if repository is None:
        if not dsn:
            raise RuntimeError("PRD_AGENT_DATABASE_DSN is required")
        production_database = ProductionDatabase.from_dsn(
            dsn,
            min_size=int(os.environ.get("PRD_AGENT_DB_POOL_MIN", "2")),
            max_size=int(os.environ.get("PRD_AGENT_DB_POOL_MAX", "10")),
        )
        repository = production_database.scoped_repository(
            PostgresWorkflowRepository
        )
        if production_control_store is None:
            production_control_store = production_database.scoped_repository(
                PostgresProductionControlStore
            )
    managed_connections = ()
    if workflow_service is None:
        if not dsn:
            raise RuntimeError(
                "PRD_AGENT_DATABASE_DSN is required for default Agent Core"
            )
        (
            workflow_service,
            investigation_reader,
            managed_connections,
        ) = _default_agent_core(
            repository,
            dsn,
        )

    @asynccontextmanager
    async def lifespan(_app):
        if production_database is not None:
            production_database.open()
        try:
            yield
        finally:
            for connection in managed_connections:
                connection.close()
            if production_database is not None:
                production_database.close()

    app = FastAPI(
        title="PRD Agent API",
        version="0.5.0-step10",
        description=(
            "Owner-scoped PRD Agent API with grounded investigation and "
            "controlled external integrations."
        ),
        lifespan=lifespan,
    )
    app.state.repository = repository
    app.state.workflow = workflow_service
    app.state.investigation_reader = investigation_reader
    app.state.export_service = export_service
    app.state.remote_repository_catalog = remote_repository_catalog
    environment = os.environ.get("PRD_AGENT_ENVIRONMENT", "local").lower()
    if principal_resolver is None and environment == "production":
        issuer = os.environ.get("PRD_AGENT_OIDC_ISSUER", "")
        audience = os.environ.get("PRD_AGENT_OIDC_AUDIENCE", "")
        jwks_url = os.environ.get("PRD_AGENT_OIDC_JWKS_URL", "")
        if production_database is None or not issuer or not audience or not jwks_url:
            raise RuntimeError(
                "Production requires database pooling and OIDC issuer, audience, "
                "and JWKS URL configuration"
            )
        import jwt

        principal_resolver = OidcJwtPrincipalResolver(
            issuer=issuer,
            audience=audience,
            jwk_client=jwt.PyJWKClient(jwks_url),
            identity_directory=PostgresIdentityDirectory(
                production_database.pool
            ),
        )
    app.state.principal_resolver = principal_resolver or LocalPrincipalResolver()
    app.state.production_database = production_database
    app.state.production_control_store = production_control_store
    app.state.cancellation_signal = cancellation_signal
    app.state.managed_connections = managed_connections
    app.state.sse_poll_seconds = 1.0
    app.state.sse_heartbeat_seconds = 15.0
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            item.strip()
            for item in os.environ.get(
                "PRD_AGENT_CORS_ORIGINS",
                "http://localhost:3000,http://127.0.0.1:3000",
            ).split(",")
            if item.strip()
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "Last-Event-ID",
            "X-Request-ID",
        ],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        with ExitStack() as stack:
            if hasattr(repository, "bind"):
                stack.enter_context(repository.bind())
            if (
                production_control_store is not None
                and production_control_store is not repository
                and hasattr(production_control_store, "bind")
            ):
                stack.enter_context(production_control_store.bind())
            supplied = request.headers.get("x-request-id", "")
            request.state.request_id = (
                supplied
                if 1 <= len(supplied) <= 128
                and supplied.replace("-", "").isalnum()
                else f"req-{uuid.uuid4().hex}"
            )
            try:
                request.state.principal = app.state.principal_resolver.resolve(
                    request.headers.get("authorization")
                )
            except AuthenticationError:
                if request.url.path.startswith("/api/v1/health/"):
                    response = await call_next(request)
                    response.headers["X-Request-ID"] = request.state.request_id
                    return response
                response = JSONResponse(
                    status_code=401,
                    content=_error_payload(
                        request,
                        status=401,
                        code="UNAUTHORIZED",
                        title="Authentication required",
                        message="请先登录后重试。",
                    ),
                )
                response.headers["X-Request-ID"] = request.state.request_id
                return response
            response = await call_next(request)
            response.headers["X-Request-ID"] = request.state.request_id
            return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                request,
                status=422,
                code="VALIDATION_ERROR",
                title="Request validation failed",
                message="请求参数不合法，请检查后重试。",
            ),
        )

    @app.exception_handler(WorkflowError)
    async def workflow_error(request: Request, exc: WorkflowError):
        if isinstance(exc, NotFound):
            status, code, title, retryable = 404, "RESOURCE_NOT_FOUND", "Resource not found", False
        elif isinstance(exc, VersionConflict):
            status, code, title, retryable = 409, "TASK_VERSION_CONFLICT", "Task version conflict", True
        elif isinstance(exc, IdempotencyConflict):
            status, code, title, retryable = 409, "IDEMPOTENCY_CONFLICT", "Idempotency conflict", False
        elif isinstance(exc, ActiveRunConflict):
            status, code, title, retryable = 409, "ACTIVE_RUN_CONFLICT", "Active run conflict", True
        elif isinstance(exc, InvalidTransition):
            status, code, title, retryable = 409, "INVALID_TRANSITION", "Invalid transition", False
        elif isinstance(exc, InvalidCommand):
            status, code, title, retryable = 422, "INVALID_COMMAND", "Invalid command", False
        elif isinstance(exc, ModelOutputError):
            status, code, title, retryable = 503, "MODEL_OUTPUT_ERROR", "Model output failed", True
        else:
            status, code, title, retryable = 500, "WORKFLOW_ERROR", "Workflow failed", False
        return JSONResponse(
            status_code=status,
            content=_error_payload(
                request,
                status=status,
                code=code,
                title=title,
                message=(
                    "任务已更新，请刷新后重试。"
                    if isinstance(exc, VersionConflict)
                    else "请求无法完成，请检查当前任务状态。"
                ),
                retryable=retryable,
            ),
        )

    @app.exception_handler(IntegrationError)
    async def integration_error(request: Request, exc: IntegrationError):
        if exc.code == IntegrationErrorCode.RESOURCE_NOT_FOUND:
            status = 404
        elif exc.code == IntegrationErrorCode.UNAUTHORIZED:
            status = 401
        elif exc.code == IntegrationErrorCode.RATE_LIMITED:
            status = 429
        elif exc.code in {
            IntegrationErrorCode.RESOURCE_CHANGED,
            IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
            IntegrationErrorCode.INVALID_STATE,
            IntegrationErrorCode.CONFIRMATION_REQUIRED,
        }:
            status = 409
        elif exc.code in {
            IntegrationErrorCode.TIMEOUT,
            IntegrationErrorCode.PROVIDER_UNAVAILABLE,
            IntegrationErrorCode.RESULT_UNKNOWN,
        }:
            status = 503
        else:
            status = 422
        return JSONResponse(
            status_code=status,
            content=_error_payload(
                request,
                status=status,
                code=exc.code.value,
                title="External integration failed",
                message=str(exc),
                retryable=exc.retryable,
            ),
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, _exc: Exception):
        _rollback(app.state.repository)
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                request,
                status=500,
                code="INTERNAL_ERROR",
                title="Internal server error",
                message="服务暂时无法完成请求，请稍后重试。",
                retryable=True,
            ),
        )

    @app.exception_handler(AuthorizationError)
    async def authorization_error(request: Request, _exc: AuthorizationError):
        return JSONResponse(
            status_code=403,
            content=_error_payload(
                request,
                status=403,
                code="FORBIDDEN",
                title="Access denied",
                message="当前身份无权执行此操作。",
            ),
        )

    @app.get("/api/v1/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/api/v1/health/ready")
    def ready():
        if production_database is not None:
            if not production_database.readiness(
                expected_migration="20260727_step10_production_profile"
            ):
                return JSONResponse(
                    status_code=503,
                    content={"status": "unavailable"},
                )
            return {"status": "ready"}
        try:
            repository.list_tasks(LOCAL_USER, limit=1)
            repository.commit()
        except Exception:
            _rollback(repository)
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ready"}

    @app.get("/api/v1/me")
    def current_principal(request: Request):
        principal = request.state.principal
        return {
            "principal_type": principal.principal_type.value,
            "user_id": principal.user_id,
            "roles": sorted(principal.roles),
            "scopes": sorted(principal.scopes),
        }

    @app.get("/api/v1/tasks", response_model=TaskListResponse)
    def list_task_route(
        request: Request,
        cursor: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
    ):
        actor_id = _actor(request, "tasks:read")
        try:
            result = task_list(repository, actor_id, cursor, limit)
            repository.commit()
            return result
        except Exception:
            _rollback(repository)
            raise

    @app.get(
        "/api/v1/repository-bindings",
        response_model=RepositoryBindingListResponse,
    )
    def list_repository_bindings_route(request: Request):
        actor_id = _actor(request, "repositories:read")
        catalog = app.state.remote_repository_catalog
        if catalog is None:
            return RepositoryBindingListResponse(items=[])
        return RepositoryBindingListResponse(
            items=[
                RepositoryBindingView(
                    repository_id=binding.repository_id,
                    provider=binding.provider.value,
                    display_name=binding.display_name,
                    default_revision=binding.default_revision,
                    allowed_prefix=binding.allowed_prefix,
                )
                for binding in catalog.list_for_owner(actor_id)
            ]
        )

    @app.post(
        "/api/v1/tasks/from-message",
        response_model=TaskDetailResponse,
        status_code=201,
    )
    def start_task_route(
        request: Request,
        body: StartTaskRequest,
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        actor_id = _actor(request, "tasks:write")
        snapshot = _execute(
            app,
            lambda: workflow_service.start_task(
                StartTask(
                    body.message,
                    idempotency_key,
                    actor_id=actor_id,
                )
            ),
        )
        records = (
            app.state.investigation_reader.for_task(snapshot.task.task_id)
            if app.state.investigation_reader is not None
            else ()
        )
        return task_detail(
            snapshot,
            repository.latest_event_sequence(snapshot.task.task_id, actor_id),
            records,
        )

    @app.get("/api/v1/tasks/{task_id}", response_model=TaskDetailResponse)
    def get_task_route(request: Request, task_id: str):
        return _detail(app, task_id, _actor(request, "tasks:read"))

    @app.get("/api/v1/tasks/{task_id}/runs")
    def list_runs_route(request: Request, task_id: str):
        actor_id = _actor(request, "tasks:read")
        repository.snapshot_for_owner(task_id, actor_id)
        repository.commit()
        if app.state.production_control_store is None:
            return {"items": []}
        runs = app.state.production_control_store.list_runs(task_id, actor_id)
        return {
            "items": [
                {
                    "run_id": run.run_id,
                    "task_id": run.task_id,
                    "status": run.status.value,
                    "attempt_count": run.attempt_count,
                    "cancellation_requested_at": (
                        run.cancellation_requested_at.isoformat()
                        if run.cancellation_requested_at
                        else None
                    ),
                }
                for run in runs
            ]
        }

    @app.post("/api/v1/tasks/{task_id}/runs/{run_id}/stop")
    def stop_run_route(
        request: Request,
        task_id: str,
        run_id: str,
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        del idempotency_key
        actor_id = _actor(request, "tasks:write")
        repository.snapshot_for_owner(task_id, actor_id)
        repository.commit()
        store = app.state.production_control_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content=_error_payload(
                    request,
                    status=503,
                    code="PRODUCTION_CONTROL_UNAVAILABLE",
                    title="Production control unavailable",
                    message="后台执行控制暂不可用。",
                    retryable=True,
                ),
            )
        try:
            current = store.get_run(run_id)
        except KeyError as exc:
            raise NotFound(f"run not found: {run_id}") from exc
        if current.task_id != task_id or current.owner_id != actor_id:
            raise NotFound(f"run not found: {run_id}")
        run = store.request_cancellation(
            run_id,
            owner_id=actor_id,
            now=datetime.now(timezone.utc),
        )
        if app.state.cancellation_signal is not None:
            try:
                app.state.cancellation_signal.notify(run_id)
            except Exception:
                # PostgreSQL cancellation is authoritative; Redis is acceleration.
                pass
        return {
            "run_id": run.run_id,
            "task_id": run.task_id,
            "status": run.status.value,
            "cancellation_requested_at": (
                run.cancellation_requested_at.isoformat()
                if run.cancellation_requested_at
                else None
            ),
        }

    @app.post(
        "/api/v1/tasks/{task_id}/runs/{run_id}/retry",
        status_code=202,
    )
    def retry_run_route(
        request: Request,
        task_id: str,
        run_id: str,
        body: RetryRunRequest,
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        del idempotency_key
        actor_id = _actor(request, "tasks:write")
        snapshot = repository.snapshot_for_owner(task_id, actor_id)
        if snapshot.task.version != body.expected_task_version:
            raise VersionConflict("task version changed before retry")
        repository.commit()
        store = app.state.production_control_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content=_error_payload(
                    request,
                    status=503,
                    code="PRODUCTION_CONTROL_UNAVAILABLE",
                    title="Production control unavailable",
                    message="后台执行控制暂不可用。",
                    retryable=True,
                ),
            )
        try:
            current = store.get_run(run_id)
            if current.task_id != task_id or current.owner_id != actor_id:
                raise KeyError(run_id)
            run = store.retry_run(
                run_id,
                owner_id=actor_id,
                now=datetime.now(timezone.utc),
            )
        except KeyError as exc:
            raise NotFound(f"run not found: {run_id}") from exc
        except ValueError as exc:
            raise InvalidTransition(str(exc)) from exc
        return {
            "run_id": run.run_id,
            "task_id": run.task_id,
            "status": run.status.value,
            "attempt_count": run.attempt_count,
        }

    @app.post(
        "/api/v1/tasks/{task_id}/messages",
        response_model=TaskDetailResponse,
    )
    def reply_route(
        request: Request,
        task_id: str,
        body: ReplyRequest,
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        actor_id = _actor(request, "tasks:write")
        _execute(
            app,
            lambda: workflow_service.reply_to_task(
                ReplyToTask(
                    task_id,
                    body.message,
                    body.expected_task_version,
                    idempotency_key,
                    actor_id=actor_id,
                )
            ),
        )
        return _detail(app, task_id, actor_id)

    @app.post(
        "/api/v1/tasks/{task_id}/outline/confirm",
        response_model=TaskDetailResponse,
    )
    def confirm_outline_route(
        request: Request,
        task_id: str,
        body: ConfirmOutlineRequest,
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        actor_id = _actor(request, "tasks:write")
        _execute(
            app,
            lambda: workflow_service.confirm_outline(
                ConfirmOutline(
                    task_id,
                    body.outline_version,
                    body.expected_task_version,
                    idempotency_key,
                    actor_id=actor_id,
                )
            ),
        )
        return _detail(app, task_id, actor_id)

    @app.post(
        "/api/v1/tasks/{task_id}/units/{unit_id}/confirm",
        response_model=TaskDetailResponse,
    )
    def confirm_unit_route(
        request: Request,
        task_id: str,
        unit_id: str,
        body: ConfirmUnitRequest,
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        actor_id = _actor(request, "tasks:write")
        _execute(
            app,
            lambda: workflow_service.confirm_unit(
                ConfirmUnit(
                    task_id,
                    unit_id,
                    body.expected_task_version,
                    idempotency_key,
                    actor_id=actor_id,
                )
            ),
        )
        return _detail(app, task_id, actor_id)

    @app.post(
        "/api/v1/tasks/{task_id}/revisions/approve",
        response_model=TaskDetailResponse,
    )
    def approve_revision_route(
        request: Request,
        task_id: str,
        body: ApproveRevisionRequest,
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        actor_id = _actor(request, "tasks:write")
        _execute(
            app,
            lambda: workflow_service.approve_revision_plan(
                ApproveRevisionPlan(
                    task_id,
                    body.document_id,
                    body.issue_ids,
                    body.unit_ids,
                    body.expected_task_version,
                    idempotency_key,
                    actor_id=actor_id,
                )
            ),
        )
        return _detail(app, task_id, actor_id)

    @app.post(
        "/api/v1/tasks/{task_id}/finalize",
        response_model=TaskDetailResponse,
    )
    def finalize_route(
        request: Request,
        task_id: str,
        body: FinalizeRequest,
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        actor_id = _actor(request, "tasks:write")
        _execute(
            app,
            lambda: workflow_service.finalize_prd(
                FinalizePrd(
                    task_id,
                    body.document_id,
                    body.content_hash,
                    body.expected_task_version,
                    idempotency_key,
                    actor_id=actor_id,
                )
            ),
        )
        return _detail(app, task_id, actor_id)

    @app.post(
        "/api/v1/tasks/{task_id}/reopen",
        response_model=TaskDetailResponse,
    )
    def reopen_route(
        request: Request,
        task_id: str,
        body: ReopenRequest,
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        actor_id = _actor(request, "tasks:write")
        _execute(
            app,
            lambda: workflow_service.reopen_prd(
                ReopenPrd(
                    task_id,
                    body.unit_ids,
                    body.reason,
                    body.expected_task_version,
                    idempotency_key,
                    actor_id=actor_id,
                )
            ),
        )
        return _detail(app, task_id, actor_id)

    @app.post(
        "/api/v1/tasks/{task_id}/exports/feishu/preview",
        response_model=ExportPreviewView,
    )
    def preview_feishu_export_route(
        request: Request,
        task_id: str,
        body: ExportPreviewRequest,
        idempotency_key: str = Header(
            min_length=1,
            max_length=200,
            alias="Idempotency-Key",
        ),
    ):
        actor_id = _actor(request, "exports:write")
        if app.state.export_service is None:
            raise IntegrationError(
                IntegrationErrorCode.INVALID_STATE,
                "Feishu export is not configured",
            )
        value = _execute(
            app,
            lambda: app.state.export_service.preview(
                task_id=task_id,
                owner_id=actor_id,
                mode=body.mode,
                expected_task_version=body.expected_task_version,
                idempotency_key=idempotency_key,
            ),
        )
        return ExportPreviewView(
            **value.model_dump(exclude={"mode", "unresolved_items"}),
            mode=value.mode.value,
            unresolved_items=list(value.unresolved_items),
        )

    @app.post(
        "/api/v1/tasks/{task_id}/exports/feishu",
        response_model=ExportRunView,
    )
    def execute_feishu_export_route(
        request: Request,
        task_id: str,
        body: ExecuteExportRequest,
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        actor_id = _actor(request, "exports:write")
        if app.state.export_service is None:
            raise IntegrationError(
                IntegrationErrorCode.INVALID_STATE,
                "Feishu export is not configured",
            )
        value = _execute(
            app,
            lambda: app.state.export_service.execute(
                intent_id=body.intent_id,
                confirmation_token=body.confirmation_token,
                owner_id=actor_id,
                expected_task_version=body.expected_task_version,
                idempotency_key=idempotency_key,
                mode=body.mode,
                expected_task_id=task_id,
            ),
        )
        return ExportRunView(
            **value.model_dump(
                exclude={"task_id", "owner_id", "mode", "idempotency_key_hash"}
            ),
            mode=value.mode.value,
        )

    @app.get(
        "/api/v1/tasks/{task_id}/exports",
        response_model=ExportListResponse,
    )
    def list_exports_route(request: Request, task_id: str):
        actor_id = _actor(request, "tasks:read")
        if app.state.export_service is None:
            return ExportListResponse(items=[])
        values = app.state.export_service.list_exports(
            task_id,
            actor_id,
        )
        return ExportListResponse(
            items=[
                ExportRunView(
                    **value.model_dump(
                        exclude={
                            "task_id",
                            "owner_id",
                            "mode",
                            "idempotency_key_hash",
                        }
                    ),
                    mode=value.mode.value,
                )
                for value in values
            ]
        )

    @app.get("/api/v1/tasks/{task_id}/events")
    async def events_route(
        request: Request,
        task_id: str,
        after_sequence: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        actor_id = _actor(request, "tasks:read")
        repository.snapshot_for_owner(task_id, actor_id)
        repository.commit()
        if last_event_id:
            try:
                after_sequence = max(after_sequence, int(last_event_id))
            except ValueError as exc:
                raise InvalidCommand("Last-Event-ID must be an integer") from exc

        async def stream():
            current = after_sequence
            idle = 0.0
            while not await request.is_disconnected():
                with ExitStack() as poll_stack:
                    if hasattr(repository, "bind"):
                        poll_stack.enter_context(repository.bind())
                    events = repository.list_events(
                        task_id,
                        actor_id,
                        after_sequence=current,
                        limit=100,
                    )
                    repository.commit()
                if events:
                    idle = 0.0
                    for event in events:
                        current = event.sequence
                        public_name = PUBLIC_EVENT_NAMES.get(event.event_type)
                        if public_name is None:
                            continue
                        data = {
                            "event_id": event.event_id,
                            "task_id": event.task_id,
                            "sequence": event.sequence,
                            "event": public_name,
                            "occurred_at": event.created_at.isoformat(),
                            "payload": public_event_payload(
                                event.event_type,
                                dict(event.payload),
                            ),
                        }
                        yield (
                            f"id: {event.sequence}\n"
                            f"event: {public_name}\n"
                            "retry: 3000\n"
                            f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                        )
                else:
                    await asyncio.sleep(app.state.sse_poll_seconds)
                    idle += app.state.sse_poll_seconds
                    if idle >= app.state.sse_heartbeat_seconds:
                        idle = 0.0
                        yield ": heartbeat\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app
