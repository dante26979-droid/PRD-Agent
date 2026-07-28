class RepositoryError(Exception):
    code = "REPOSITORY_ERROR"


class UnknownRepository(RepositoryError):
    code = "UNKNOWN_REPOSITORY"


class InvalidRevision(RepositoryError):
    code = "INVALID_REVISION"


class BlockedPath(RepositoryError):
    code = "BLOCKED_PATH"


class BlobNotFound(RepositoryError):
    code = "BLOB_NOT_FOUND"


class BinaryFileBlocked(RepositoryError):
    code = "BLOCKED_FILE_TYPE"


class FileTooLarge(RepositoryError):
    code = "FILE_TOO_LARGE"

