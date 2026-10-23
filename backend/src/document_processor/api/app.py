"""FastAPI application factory."""

from collections.abc import Sequence
from typing import Annotated

from fastapi import FastAPI, File, Form, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from document_processor.api.schemas import (
    AcceptedDocumentResponse,
    BatchAcceptedResponse,
    ErrorDetail,
    ErrorResponse,
)
from document_processor.config import Settings
from document_processor.domain.errors import (
    DocumentProcessorError,
    FileTooLargeError,
    InfrastructureUnavailableError,
    ProviderUnavailableError,
    SubmissionError,
    TooManyFilesError,
)
from document_processor.domain.models import NewDocument, ParserKind, UploadCandidate
from document_processor.ports import SubmissionGateway
from document_processor.services.submission import SubmissionService

READ_CHUNK_BYTES = 64 * 1024


class UnconfiguredSubmissionGateway:
    async def create_batch(self, documents: Sequence[NewDocument]) -> None:
        del documents
        raise InfrastructureUnavailableError("Document queue")


def _error_response(status_code: int, error: DocumentProcessorError) -> JSONResponse:
    payload = ErrorResponse(error=ErrorDetail(code=error.code, message=str(error)))
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


def create_app(
    *,
    settings: Settings | None = None,
    submission_gateway: SubmissionGateway | None = None,
) -> FastAPI:
    app_settings = settings or Settings()
    gateway = submission_gateway or UnconfiguredSubmissionGateway()
    service = SubmissionService(gateway=gateway, settings=app_settings)

    app = FastAPI(
        title="Async document processor",
        version="0.1.0",
        description="Upload PDFs for asynchronous extraction and summarization.",
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: object,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "Provide at least one PDF and a supported parser.",
                }
            },
        )

    @app.exception_handler(DocumentProcessorError)
    async def document_processor_error(
        _request: object,
        error: DocumentProcessorError,
    ) -> JSONResponse:
        if isinstance(error, (TooManyFilesError, FileTooLargeError)):
            return _error_response(status.HTTP_413_CONTENT_TOO_LARGE, error)
        if isinstance(error, (ProviderUnavailableError, InfrastructureUnavailableError)):
            return _error_response(status.HTTP_503_SERVICE_UNAVAILABLE, error)
        if isinstance(error, SubmissionError):
            return _error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, error)
        return _error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, error)

    @app.post(
        "/api/v1/documents",
        response_model=BatchAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses={
            413: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def submit_documents(
        parser: Annotated[ParserKind, Form()],
        files: Annotated[list[UploadFile], File()],
    ) -> BatchAcceptedResponse:
        if len(files) > app_settings.max_files_per_batch:
            raise TooManyFilesError(app_settings.max_files_per_batch)

        candidates: list[UploadCandidate] = []
        try:
            for upload in files:
                content = await _read_bounded(upload, app_settings.max_file_size_bytes)
                candidates.append(
                    UploadCandidate(
                        filename=upload.filename or "document.pdf",
                        content_type=upload.content_type or "",
                        content=content,
                    )
                )
        finally:
            for upload in files:
                await upload.close()

        accepted = await service.submit(candidates, parser)
        return BatchAcceptedResponse(
            documents=[
                AcceptedDocumentResponse(
                    id=document.id,
                    filename=document.filename,
                    parser=document.parser,
                    status=document.status,
                    created_at=document.created_at,
                )
                for document in accepted
            ]
        )

    return app


async def _read_bounded(upload: UploadFile, maximum: int) -> bytes:
    content = bytearray()
    while chunk := await upload.read(READ_CHUNK_BYTES):
        content.extend(chunk)
        if len(content) > maximum:
            raise FileTooLargeError(upload.filename or "document.pdf", maximum)
    return bytes(content)


app = create_app()
