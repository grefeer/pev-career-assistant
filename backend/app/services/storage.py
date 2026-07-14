from __future__ import annotations

import base64
import binascii
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from botocore.exceptions import ClientError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENCRYPTION_VERSION = "v1-aes-256-gcm"
NONCE_SIZE = 12
logger = logging.getLogger(__name__)


class BlobStore(Protocol):
    def put_bytes(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> None: ...

    def get_bytes(self, *, key: str) -> bytes: ...

    def delete(self, *, key: str) -> None: ...

    def head(self, *, key: str) -> Mapping[str, Any]: ...

    def ensure_bucket(self) -> None: ...

    def check_bucket(self) -> None: ...


@dataclass(frozen=True)
class StoredObject:
    key: str
    content_type: str
    plaintext_size: int
    encryption: str = ENCRYPTION_VERSION


class S3BlobStore:
    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def put_bytes(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            Metadata=metadata,
        )

    def get_bytes(self, *, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        return response["Body"].read()

    def delete(self, *, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def head(self, *, key: str) -> Mapping[str, Any]:
        return self._client.head_object(Bucket=self._bucket, Key=key)

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            self._client.create_bucket(Bucket=self._bucket)

    def check_bucket(self) -> None:
        self._client.head_bucket(Bucket=self._bucket)


class EncryptedObjectStore:
    def __init__(self, blob_store: BlobStore, encryption_key: str) -> None:
        try:
            key = base64.b64decode(encryption_key, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "OBJECT_ENCRYPTION_KEY must be a 32-byte base64 value"
            ) from exc
        if len(key) != 32:
            raise ValueError("OBJECT_ENCRYPTION_KEY must be a 32-byte base64 value")
        self._blob_store = blob_store
        self._cipher = AESGCM(key)

    def put(self, *, key: str, plaintext: bytes, content_type: str) -> StoredObject:
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = self._cipher.encrypt(nonce, plaintext, key.encode("utf-8"))
        try:
            self._blob_store.put_bytes(
                key=key,
                body=nonce + ciphertext,
                content_type=content_type,
                metadata={"encryption": ENCRYPTION_VERSION},
            )
        except Exception:
            logger.error("encrypted object write failed")
            raise
        return StoredObject(
            key=key,
            content_type=content_type,
            plaintext_size=len(plaintext),
        )

    def get(self, *, key: str) -> bytes:
        encrypted = self._blob_store.get_bytes(key=key)
        nonce = encrypted[:NONCE_SIZE]
        ciphertext = encrypted[NONCE_SIZE:]
        if len(nonce) != NONCE_SIZE:
            raise InvalidTag
        return self._cipher.decrypt(nonce, ciphertext, key.encode("utf-8"))
