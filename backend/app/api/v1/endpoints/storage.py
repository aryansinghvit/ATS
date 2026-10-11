"""Local-disk stand-in for S3 pre-signed URLs (STORAGE_BACKEND=local, development/test only)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import FileResponse

from app.core import signing
from app.core.exceptions import NotFoundError, PermissionDeniedError, ServiceError
from app.services.storage import (
    EXTENSION_MIME_TYPES,
    LOCAL_GET_PURPOSE,
    LOCAL_PUT_PURPOSE,
    LocalStorageBackend,
    StorageService,
    get_storage_service,
)

router = APIRouter(prefix="/storage/local", tags=["storage"])

Storage = Annotated[StorageService, Depends(get_storage_service)]


class PayloadTooLargeError(ServiceError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE


class UnsupportedMediaTypeError(ServiceError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE


def _local_backend(storage: StorageService) -> LocalStorageBackend:
    if not isinstance(storage.backend, LocalStorageBackend):
        raise NotFoundError("Not found")
    return storage.backend


@router.put("/{key:path}", status_code=status.HTTP_204_NO_CONTENT)
async def upload_object(
    key: str,
    request: Request,
    storage: Storage,
    expires: int,
    content_type: str,
    signature: str,
) -> Response:
    backend = _local_backend(storage)
    if signing.is_expired(expires) or not signing.verify(
        signature, LOCAL_PUT_PURPOSE, key, content_type, expires
    ):
        raise PermissionDeniedError("Upload URL is invalid or has expired")
    sent_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if sent_type != content_type:
        raise UnsupportedMediaTypeError(f"Content-Type must be {content_type}")
    limit = storage.max_resume_size_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError("File exceeds the maximum upload size")
    try:
        await backend.write_stream(key, request.stream(), max_size_bytes=limit)
    except ValueError:
        raise PayloadTooLargeError("File exceeds the maximum upload size") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{key:path}")
async def download_object(key: str, storage: Storage, expires: int, signature: str) -> FileResponse:
    backend = _local_backend(storage)
    if signing.is_expired(expires) or not signing.verify(
        signature, LOCAL_GET_PURPOSE, key, expires
    ):
        raise PermissionDeniedError("Download URL is invalid or has expired")
    path = backend.path_for(key)
    if not path.is_file():
        raise NotFoundError("Not found")
    media_type = EXTENSION_MIME_TYPES.get(path.suffix.lstrip("."), "application/octet-stream")
    return FileResponse(path, media_type=media_type)
