from __future__ import annotations

from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode

from .models import ExportSourceSnapshot


class WorkflowExportDocumentSource:
    """Read the immutable, owner-scoped final document from the workflow store."""

    def __init__(self, workflow_repository) -> None:
        self.workflow_repository = workflow_repository

    def get_export_source(
        self,
        task_id: str,
        owner_id: str,
    ) -> ExportSourceSnapshot:
        snapshot = self.workflow_repository.snapshot_for_owner(task_id, owner_id)
        if not snapshot.documents:
            raise IntegrationError(
                IntegrationErrorCode.INVALID_STATE,
                "task has no PRD document",
            )
        document = snapshot.documents[-1]
        unresolved_items = (
            snapshot.current_brief.brief.open_questions
            if snapshot.current_brief
            else ()
        )
        return ExportSourceSnapshot(
            task_id=task_id,
            owner_id=owner_id,
            task_status=snapshot.task.status.value,
            task_version=snapshot.task.version,
            document_id=document.document_id,
            document_version=document.version,
            markdown=document.markdown,
            unresolved_items=unresolved_items,
        )
