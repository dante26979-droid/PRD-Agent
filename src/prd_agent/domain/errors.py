class WorkflowError(Exception):
    """Base class for public workflow errors."""


class InvalidCommand(WorkflowError):
    pass


class InvalidTransition(WorkflowError):
    pass


class VersionConflict(WorkflowError):
    pass


class ActiveRunConflict(WorkflowError):
    pass


class IdempotencyConflict(WorkflowError):
    pass


class NotFound(WorkflowError):
    pass


class ModelOutputError(WorkflowError):
    pass

