"""Safe domain errors exposed through application boundaries."""


class DocumentProcessorError(Exception):
    """Base error with a stable machine-readable code."""

    code = "document_processor_error"


class SubmissionError(DocumentProcessorError):
    """Base class for submission validation failures."""


class EmptyBatchError(SubmissionError):
    code = "empty_batch"

    def __init__(self) -> None:
        super().__init__("At least one PDF file is required.")


class TooManyFilesError(SubmissionError):
    code = "too_many_files"

    def __init__(self, maximum: int) -> None:
        super().__init__(f"A batch can contain at most {maximum} PDF files.")


class FileTooLargeError(SubmissionError):
    code = "file_too_large"

    def __init__(self, filename: str, maximum: int) -> None:
        super().__init__(f"{filename} exceeds the {maximum}-byte file limit.")


class InvalidMediaTypeError(SubmissionError):
    code = "invalid_media_type"

    def __init__(self, filename: str) -> None:
        super().__init__(f"{filename} must use the application/pdf media type.")


class InvalidPdfError(SubmissionError):
    code = "invalid_pdf"

    def __init__(self, filename: str) -> None:
        super().__init__(f"{filename} does not have a valid PDF signature.")


class ProviderUnavailableError(SubmissionError):
    code = "provider_unavailable"

    def __init__(self, provider: str) -> None:
        super().__init__(f"{provider} is not configured for this processing mode.")


class InfrastructureUnavailableError(DocumentProcessorError):
    code = "infrastructure_unavailable"

    def __init__(self, resource: str) -> None:
        super().__init__(f"{resource} is temporarily unavailable.")


class DocumentNotFoundError(DocumentProcessorError):
    code = "document_not_found"

    def __init__(self) -> None:
        super().__init__("Document not found or expired.")


class ProcessingError(DocumentProcessorError):
    """Base class for safe worker-facing failures."""


class PermanentProcessingError(ProcessingError):
    """A document or response failure that must not be retried."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RetryableProcessingError(ProcessingError):
    """A temporary provider or infrastructure failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)
