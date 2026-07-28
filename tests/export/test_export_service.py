from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from prd_agent.export.models import (
    CreateIdempotencyCapability,
    ExportMode,
    ExportSourceSnapshot,
    ProviderDocumentResult,
)
from prd_agent.export.service import ExportApplicationService
from prd_agent.export.store import InMemoryExportStore
from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode


class FakeDocumentSource:
    def __init__(self) -> None:
        self.snapshot = ExportSourceSnapshot(
            task_id="task-1",
            owner_id="local-user",
            task_status="COMPLETED",
            task_version=7,
            document_id="document-1",
            document_version=3,
            markdown="# 订单筛选\n\n支持创建时间筛选。",
        )

    def get_export_source(self, task_id, owner_id):
        assert (task_id, owner_id) == ("task-1", "local-user")
        return self.snapshot


class FakeFeishuGateway:
    create_idempotency_capability = CreateIdempotencyCapability.PROVIDER_KEY

    def __init__(self) -> None:
        self.created = []

    def create_document(self, document, *, idempotency_key):
        self.created.append((document, idempotency_key))
        return ProviderDocumentResult(
            external_id="feishu-doc-1",
            safe_url="https://example.feishu.cn/docx/feishu-doc-1",
            title=document.title,
            provider_revision="revision-1",
        )

    def overwrite_document(
        self,
        external_id,
        document,
        *,
        idempotency_key,
        expected_revision,
    ):
        raise AssertionError("overwrite should not be called")


def test_completed_prd_can_be_previewed_confirmed_and_created_idempotently():
    source = FakeDocumentSource()
    gateway = FakeFeishuGateway()
    service = ExportApplicationService(
        source,
        InMemoryExportStore(),
        gateway,
        confirmation_secret=b"test-confirmation-secret",
    )

    preview = service.preview(
        task_id="task-1",
        owner_id="local-user",
        mode=ExportMode.CREATE,
        expected_task_version=7,
    )
    first = service.execute(
        intent_id=preview.intent_id,
        confirmation_token=preview.confirmation_token,
        owner_id="local-user",
        expected_task_version=7,
        idempotency_key="export-key-1",
    )
    replay = service.execute(
        intent_id=preview.intent_id,
        confirmation_token=preview.confirmation_token,
        owner_id="local-user",
        expected_task_version=7,
        idempotency_key="export-key-1",
    )

    assert preview.title == "订单筛选"
    assert first.status == "SUCCEEDED"
    assert replay.export_run_id == first.export_run_id
    assert len(gateway.created) == 1
    assert service.list_exports("task-1", "local-user")[0].binding_id


def test_preview_is_rejected_if_the_task_version_changes_before_write():
    source = FakeDocumentSource()
    gateway = FakeFeishuGateway()
    service = ExportApplicationService(
        source,
        InMemoryExportStore(),
        gateway,
        confirmation_secret=b"test-confirmation-secret",
    )
    preview = service.preview(
        task_id="task-1",
        owner_id="local-user",
        mode=ExportMode.CREATE,
        expected_task_version=7,
    )
    source.snapshot = source.snapshot.model_copy(
        update={
            "task_version": 8,
            "document_version": 4,
            "markdown": "# 已修改\n\n新版本。",
        }
    )

    try:
        service.execute(
            intent_id=preview.intent_id,
            confirmation_token=preview.confirmation_token,
            owner_id="local-user",
            expected_task_version=7,
            idempotency_key="stale-export",
        )
    except IntegrationError as exc:
        assert exc.code == IntegrationErrorCode.RESOURCE_CHANGED
    else:
        raise AssertionError("stale preview must be rejected")

    assert gateway.created == []


def test_overwrite_uses_only_the_document_bound_by_the_create_run():
    source = FakeDocumentSource()

    class OverwriteGateway(FakeFeishuGateway):
        def __init__(self):
            super().__init__()
            self.overwritten = []

        def overwrite_document(
            self,
            external_id,
            document,
            *,
            idempotency_key,
            expected_revision,
        ):
            self.overwritten.append((external_id, expected_revision))
            return ProviderDocumentResult(
                external_id=external_id,
                safe_url="https://example.feishu.cn/docx/feishu-doc-1",
                title=document.title,
                provider_revision="revision-2",
            )

    gateway = OverwriteGateway()
    service = ExportApplicationService(
        source,
        InMemoryExportStore(),
        gateway,
        confirmation_secret=b"test-confirmation-secret",
    )
    create_preview = service.preview(
        task_id="task-1",
        owner_id="local-user",
        mode=ExportMode.CREATE,
        expected_task_version=7,
    )
    service.execute(
        intent_id=create_preview.intent_id,
        confirmation_token=create_preview.confirmation_token,
        owner_id="local-user",
        expected_task_version=7,
        idempotency_key="create-first",
    )
    overwrite_preview = service.preview(
        task_id="task-1",
        owner_id="local-user",
        mode=ExportMode.OVERWRITE_BOUND,
        expected_task_version=7,
    )
    overwritten = service.execute(
        intent_id=overwrite_preview.intent_id,
        confirmation_token=overwrite_preview.confirmation_token,
        owner_id="local-user",
        expected_task_version=7,
        idempotency_key="overwrite-bound",
    )

    assert overwritten.status == "SUCCEEDED"
    assert gateway.overwritten == [("feishu-doc-1", "revision-1")]
    assert len(gateway.created) == 1


def test_unknown_create_result_without_provider_idempotency_requires_manual_review():
    source = FakeDocumentSource()

    class AmbiguousGateway(FakeFeishuGateway):
        create_idempotency_capability = CreateIdempotencyCapability.NONE

        def create_document(self, document, *, idempotency_key):
            self.created.append((document, idempotency_key))
            raise IntegrationError(
                IntegrationErrorCode.RESULT_UNKNOWN,
                "provider response was lost",
                retryable=True,
            )

    gateway = AmbiguousGateway()
    service = ExportApplicationService(
        source,
        InMemoryExportStore(),
        gateway,
        confirmation_secret=b"test-confirmation-secret",
    )
    preview = service.preview(
        task_id="task-1",
        owner_id="local-user",
        mode=ExportMode.CREATE,
        expected_task_version=7,
    )

    first = service.execute(
        intent_id=preview.intent_id,
        confirmation_token=preview.confirmation_token,
        owner_id="local-user",
        expected_task_version=7,
        idempotency_key="ambiguous-create",
    )
    replay = service.execute(
        intent_id=preview.intent_id,
        confirmation_token=preview.confirmation_token,
        owner_id="local-user",
        expected_task_version=7,
        idempotency_key="ambiguous-create",
    )

    assert first.status == replay.status == "MANUAL_REVIEW"
    assert first.retryable is False
    assert len(gateway.created) == 1


def test_concurrent_replay_reserves_one_logical_create_before_provider_call():
    source = FakeDocumentSource()

    class BlockingGateway(FakeFeishuGateway):
        def __init__(self):
            super().__init__()
            self.started = Event()
            self.release = Event()

        def create_document(self, document, *, idempotency_key):
            self.created.append((document, idempotency_key))
            self.started.set()
            assert self.release.wait(timeout=2)
            return ProviderDocumentResult(
                external_id="feishu-doc-1",
                safe_url="https://example.feishu.cn/docx/feishu-doc-1",
                title=document.title,
                provider_revision="revision-1",
            )

    gateway = BlockingGateway()
    service = ExportApplicationService(
        source,
        InMemoryExportStore(),
        gateway,
        confirmation_secret=b"test-confirmation-secret",
    )
    preview = service.preview(
        task_id="task-1",
        owner_id="local-user",
        mode=ExportMode.CREATE,
        expected_task_version=7,
    )
    arguments = {
        "intent_id": preview.intent_id,
        "confirmation_token": preview.confirmation_token,
        "owner_id": "local-user",
        "expected_task_version": 7,
        "idempotency_key": "concurrent-create",
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(service.execute, **arguments)
        assert gateway.started.wait(timeout=2)
        concurrent = executor.submit(service.execute, **arguments).result(timeout=2)
        gateway.release.set()
        first = first_future.result(timeout=2)

    assert concurrent.export_run_id == first.export_run_id
    assert concurrent.status == "RUNNING"
    assert first.status == "SUCCEEDED"
    assert len(gateway.created) == 1


def test_concurrent_different_keys_cannot_consume_the_same_confirmation_twice():
    source = FakeDocumentSource()

    class SingleCallGateway(FakeFeishuGateway):
        def __init__(self):
            super().__init__()
            self.started = Event()
            self.release = Event()

        def create_document(self, document, *, idempotency_key):
            if self.created:
                raise AssertionError("confirmation triggered a duplicate write")
            self.created.append((document, idempotency_key))
            self.started.set()
            assert self.release.wait(timeout=2)
            return ProviderDocumentResult(
                external_id="feishu-doc-1",
                safe_url="https://example.feishu.cn/docx/feishu-doc-1",
                title=document.title,
                provider_revision="revision-1",
            )

    gateway = SingleCallGateway()
    service = ExportApplicationService(
        source,
        InMemoryExportStore(),
        gateway,
        confirmation_secret=b"test-confirmation-secret",
    )
    preview = service.preview(
        task_id="task-1",
        owner_id="local-user",
        mode=ExportMode.CREATE,
        expected_task_version=7,
    )
    common = {
        "intent_id": preview.intent_id,
        "confirmation_token": preview.confirmation_token,
        "owner_id": "local-user",
        "expected_task_version": 7,
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            service.execute,
            **common,
            idempotency_key="first-command",
        )
        assert gateway.started.wait(timeout=2)
        try:
            with pytest.raises(IntegrationError) as captured:
                service.execute(
                    **common,
                    idempotency_key="different-command",
                )
            assert captured.value.code == IntegrationErrorCode.CONFIRMATION_REQUIRED
        finally:
            gateway.release.set()
        first = first_future.result(timeout=2)

    assert first.status == "SUCCEEDED"
    assert len(gateway.created) == 1


def test_retryable_provider_failure_releases_confirmation_for_a_safe_retry():
    source = FakeDocumentSource()

    class FailOnceGateway(FakeFeishuGateway):
        def create_document(self, document, *, idempotency_key):
            self.created.append((document, idempotency_key))
            if len(self.created) == 1:
                raise IntegrationError(
                    IntegrationErrorCode.RATE_LIMITED,
                    "provider asked the client to retry",
                    retryable=True,
                )
            return ProviderDocumentResult(
                external_id="feishu-doc-1",
                safe_url="https://example.feishu.cn/docx/feishu-doc-1",
                title=document.title,
                provider_revision="revision-1",
            )

    gateway = FailOnceGateway()
    service = ExportApplicationService(
        source,
        InMemoryExportStore(),
        gateway,
        confirmation_secret=b"test-confirmation-secret",
    )
    preview = service.preview(
        task_id="task-1",
        owner_id="local-user",
        mode=ExportMode.CREATE,
        expected_task_version=7,
    )
    common = {
        "intent_id": preview.intent_id,
        "confirmation_token": preview.confirmation_token,
        "owner_id": "local-user",
        "expected_task_version": 7,
    }

    failed = service.execute(
        **common,
        idempotency_key="rate-limited-attempt",
    )
    retried = service.execute(
        **common,
        idempotency_key="safe-retry",
    )

    assert failed.status == "FAILED"
    assert failed.retryable is True
    assert retried.status == "SUCCEEDED"
    assert len(gateway.created) == 2


def test_preview_idempotency_replays_the_same_single_use_intent():
    service = ExportApplicationService(
        FakeDocumentSource(),
        InMemoryExportStore(),
        FakeFeishuGateway(),
        confirmation_secret=b"test-confirmation-secret",
    )

    first = service.preview(
        task_id="task-1",
        owner_id="local-user",
        mode=ExportMode.CREATE,
        expected_task_version=7,
        idempotency_key="preview-key",
    )
    replay = service.preview(
        task_id="task-1",
        owner_id="local-user",
        mode=ExportMode.CREATE,
        expected_task_version=7,
        idempotency_key="preview-key",
    )

    assert replay.intent_id == first.intent_id
    assert replay.confirmation_token == first.confirmation_token


def test_concurrent_preview_idempotency_returns_one_confirmation_intent():
    class RacingStore(InMemoryExportStore):
        def __init__(self):
            super().__init__()
            self.initial_reads = Barrier(2)

        def get_preview_replay(self, owner_id, idempotency_key):
            value = super().get_preview_replay(owner_id, idempotency_key)
            if value is None:
                self.initial_reads.wait(timeout=2)
            return value

    service = ExportApplicationService(
        FakeDocumentSource(),
        RacingStore(),
        FakeFeishuGateway(),
        confirmation_secret=b"test-confirmation-secret",
    )
    arguments = {
        "task_id": "task-1",
        "owner_id": "local-user",
        "mode": ExportMode.CREATE,
        "expected_task_version": 7,
        "idempotency_key": "concurrent-preview-key",
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        previews = tuple(
            future.result(timeout=2)
            for future in (
                executor.submit(service.preview, **arguments),
                executor.submit(service.preview, **arguments),
            )
        )

    assert previews[0].intent_id == previews[1].intent_id
    assert previews[0].confirmation_token == previews[1].confirmation_token
