"""Resume object storage: S3 / Cloudflare R2 via aioboto3 in production, local disk for dev/test.

Both backends hand the browser a short-lived pre-signed PUT URL so resume bytes never pass
through application code. The local backend simulates S3 pre-signing with HMAC-signed URLs
served by ``/api/v1/storage/local`` (see endpoints/storage.py).

Uploaded objects are verified server-side before an application is accepted: the key must belong
to the tenant, the size must be within limits and the magic number must match the extension.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from app.core import signing
from app.core.config import Settings, get_settings
from app.core.exceptions import ServiceError, UnprocessableError

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
RESUME_MIME_TYPES: dict[str, str] = {PDF_MIME: "pdf", DOCX_MIME: "docx"}
EXTENSION_MIME_TYPES: dict[str, str] = {ext: mime for mime, ext in RESUME_MIME_TYPES.items()}
# PDF files start with "%PDF-"; DOCX files are ZIP containers ("PK\x03\x04").
MAGIC_NUMBERS: dict[str, bytes] = {"pdf": b"%PDF-", "docx": b"PK\x03\x04"}

RESUME_KEY_PATTERN = re.compile(
    r"^tenants/(?P<tenant>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"/resumes/(?P<object>[0-9a-f]{32})\.(?P<ext>pdf|docx)$"
)

LOCAL_PUT_PURPOSE = "storage-put"
LOCAL_GET_PURPOSE = "storage-get"


class StorageError(ServiceError):
    status_code = 502


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    url: str
    key: str
    expires_in: int
    max_size_bytes: int
    method: str = "PUT"
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    extension: str

    @property
    def mime_type(self) -> str:
        return EXTENSION_MIME_TYPES[self.extension]


class StorageBackend(ABC):
    @abstractmethod
    async def presign_put(self, key: str, *, content_type: str, expires_in: int) -> str: ...

    @abstractmethod
    async def presign_get(self, key: str, *, expires_in: int) -> str: ...

    @abstractmethod
    async def size_of(self, key: str) -> int | None:
        """Object size in bytes, or None when the object does not exist."""

    @abstractmethod
    async def read_head(self, key: str, length: int) -> bytes: ...

    @abstractmethod
    async def read_bytes(self, key: str) -> bytes:
        """Whole object; raises FileNotFoundError when it does not exist."""

    @abstractmethod
    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        """Server-side upload (e.g. recorded screening answers, generated offer letters)."""

    @abstractmethod
    async def delete(self, key: str) -> None: ...


# ------------------------------------------------------------------------- local backend
class LocalStorageBackend(StorageBackend):
    def __init__(self, root: Path, *, public_base_url: str) -> None:
        self.root = root
        self._base = public_base_url.rstrip("/")

    def path_for(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise UnprocessableError("Invalid storage key")
        return path

    def _url(self, key: str, params: dict[str, Any]) -> str:
        return f"{self._base}/{quote(key)}?{urlencode(params)}"

    async def presign_put(self, key: str, *, content_type: str, expires_in: int) -> str:
        expires = signing.expiry(expires_in)
        signature = signing.sign(LOCAL_PUT_PURPOSE, key, content_type, expires)
        return self._url(
            key, {"expires": expires, "content_type": content_type, "signature": signature}
        )

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        expires = signing.expiry(expires_in)
        signature = signing.sign(LOCAL_GET_PURPOSE, key, expires)
        return self._url(key, {"expires": expires, "signature": signature})

    async def write_stream(
        self, key: str, chunks: AsyncIterator[bytes], *, max_size_bytes: int
    ) -> int:
        """Stream an upload to disk atomically; raises ValueError when it exceeds the limit."""
        path = self.path_for(key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
        handle = await asyncio.to_thread(tmp.open, "wb")
        written = 0
        try:
            async for chunk in chunks:
                written += len(chunk)
                if written > max_size_bytes:
                    raise ValueError("upload exceeds the maximum size")
                await asyncio.to_thread(handle.write, chunk)
        except BaseException:
            await asyncio.to_thread(handle.close)
            await asyncio.to_thread(tmp.unlink, missing_ok=True)
            raise
        await asyncio.to_thread(handle.close)
        await asyncio.to_thread(os.replace, tmp, path)
        return written

    async def size_of(self, key: str) -> int | None:
        try:
            return (await asyncio.to_thread(self.path_for(key).stat)).st_size
        except FileNotFoundError:
            return None

    async def read_head(self, key: str, length: int) -> bytes:
        def _read() -> bytes:
            with self.path_for(key).open("rb") as handle:
                return handle.read(length)

        return await asyncio.to_thread(_read)

    async def read_bytes(self, key: str) -> bytes:
        return await asyncio.to_thread(self.path_for(key).read_bytes)

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        path = self.path_for(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
            tmp.write_bytes(data)
            os.replace(tmp, path)

        await asyncio.to_thread(_write)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.path_for(key).unlink, missing_ok=True)


# ----------------------------------------------------------------------- S3 / R2 backend
class S3StorageBackend(StorageBackend):
    def __init__(self, settings: Settings) -> None:
        import aioboto3
        from botocore.config import Config

        self._bucket = settings.S3_BUCKET or ""
        self._session = aioboto3.Session(
            aws_access_key_id=(
                settings.S3_ACCESS_KEY_ID.get_secret_value() if settings.S3_ACCESS_KEY_ID else None
            ),
            aws_secret_access_key=(
                settings.S3_SECRET_ACCESS_KEY.get_secret_value()
                if settings.S3_SECRET_ACCESS_KEY
                else None
            ),
            region_name=settings.S3_REGION
            or ("auto" if settings.STORAGE_BACKEND == "r2" else None),
        )
        self._client_kwargs: dict[str, Any] = {
            "endpoint_url": settings.S3_ENDPOINT_URL,
            "config": Config(signature_version="s3v4"),
        }

    def _client(self) -> Any:
        return self._session.client("s3", **self._client_kwargs)

    async def presign_put(self, key: str, *, content_type: str, expires_in: int) -> str:
        async with self._client() as s3:
            return await s3.generate_presigned_url(
                "put_object",
                Params={"Bucket": self._bucket, "Key": key, "ContentType": content_type},
                ExpiresIn=expires_in,
            )

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        async with self._client() as s3:
            return await s3.generate_presigned_url(
                "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=expires_in
            )

    async def size_of(self, key: str) -> int | None:
        from botocore.exceptions import ClientError

        async with self._client() as s3:
            try:
                head = await s3.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                    return None
                raise StorageError("Object storage is unavailable") from exc
            return int(head["ContentLength"])

    async def read_head(self, key: str, length: int) -> bytes:
        async with self._client() as s3:
            obj = await s3.get_object(Bucket=self._bucket, Key=key, Range=f"bytes=0-{length - 1}")
            async with obj["Body"] as body:
                return await body.read()

    async def read_bytes(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        async with self._client() as s3:
            try:
                obj = await s3.get_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                    raise FileNotFoundError(key) from exc
                raise StorageError("Object storage is unavailable") from exc
            async with obj["Body"] as body:
                return await body.read()

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        async with self._client() as s3:
            await s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    async def delete(self, key: str) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=self._bucket, Key=key)


# ------------------------------------------------------------------------------ service
class StorageService:
    def __init__(self, backend: StorageBackend, settings: Settings) -> None:
        self.backend = backend
        self._settings = settings

    @property
    def max_resume_size_bytes(self) -> int:
        return self._settings.MAX_RESUME_SIZE_BYTES

    @property
    def presigned_url_ttl_seconds(self) -> int:
        return self._settings.PRESIGNED_URL_TTL_SECONDS

    async def generate_presigned_upload_url(
        self, file_name: str, mime_type: str, tenant_id: uuid.UUID
    ) -> PresignedUpload:
        """Pre-signed PUT URL (15-minute TTL) for a candidate resume upload."""
        extension = RESUME_MIME_TYPES.get(mime_type)
        if extension is None:
            raise UnprocessableError("Only PDF and DOCX resumes are accepted")
        if Path(file_name).suffix.lower().lstrip(".") != extension:
            raise UnprocessableError("The file extension does not match its declared type")
        key = f"tenants/{tenant_id}/resumes/{uuid.uuid4().hex}.{extension}"
        ttl = self._settings.PRESIGNED_URL_TTL_SECONDS
        url = await self.backend.presign_put(key, content_type=mime_type, expires_in=ttl)
        return PresignedUpload(
            url=url,
            key=key,
            expires_in=ttl,
            max_size_bytes=self.max_resume_size_bytes,
            headers={"Content-Type": mime_type},
        )

    async def get_presigned_download_url(self, s3_key: str) -> str:
        """Expiring GET URL for viewing a candidate resume."""
        return await self.backend.presign_get(
            s3_key, expires_in=self._settings.PRESIGNED_URL_TTL_SECONDS
        )

    @staticmethod
    def parse_resume_key(s3_key: str, tenant_id: uuid.UUID) -> str:
        """Validate key shape and tenant ownership; returns the file extension."""
        match = RESUME_KEY_PATTERN.match(s3_key)
        if match is None or match["tenant"] != str(tenant_id):
            raise UnprocessableError("Invalid resume upload reference")
        return match["ext"]

    async def verify_resume_upload(self, s3_key: str, tenant_id: uuid.UUID) -> StoredObject:
        extension = self.parse_resume_key(s3_key, tenant_id)
        size = await self.backend.size_of(s3_key)
        if size is None:
            raise UnprocessableError("The resume upload was not found; please upload it again")
        if size == 0 or size > self.max_resume_size_bytes:
            await self.backend.delete(s3_key)
            limit_mb = self.max_resume_size_bytes // (1024 * 1024)
            raise UnprocessableError(f"Resumes must be between 1 byte and {limit_mb} MB")
        magic = MAGIC_NUMBERS[extension]
        if await self.backend.read_head(s3_key, len(magic)) != magic:
            await self.backend.delete(s3_key)
            raise UnprocessableError(
                "The uploaded file is not a valid PDF or DOCX document (file signature mismatch)"
            )
        return StoredObject(key=s3_key, size_bytes=size, extension=extension)

    async def delete(self, s3_key: str) -> None:
        await self.backend.delete(s3_key)


@lru_cache
def get_storage_service() -> StorageService:
    settings = get_settings()
    if settings.STORAGE_BACKEND == "local":
        backend: StorageBackend = LocalStorageBackend(
            settings.local_storage_dir,
            public_base_url=f"{settings.API_PUBLIC_URL.rstrip('/')}{settings.API_V1_PREFIX}/storage/local",
        )
    else:
        backend = S3StorageBackend(settings)
    return StorageService(backend, settings)
