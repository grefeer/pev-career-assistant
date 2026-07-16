from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag

from backend.app.services.storage import EncryptedObjectStore, S3BlobStore


@dataclass
class MemoryObject:
    body: bytes
    content_type: str
    metadata: dict[str, str]


class MemoryBlobStore:
    def __init__(self) -> None:
        self.objects: dict[str, MemoryObject] = {}

    def put_bytes(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        self.objects[key] = MemoryObject(body, content_type, metadata)

    def get_bytes(self, *, key: str) -> bytes:
        return self.objects[key].body

    def delete(self, *, key: str) -> None:
        self.objects.pop(key, None)

    def head(self, *, key: str) -> dict[str, Any]:
        item = self.objects[key]
        return {"ContentType": item.content_type, "Metadata": item.metadata}

    def ensure_bucket(self) -> None:
        return None


@pytest.fixture
def memory_blob_store() -> MemoryBlobStore:
    return MemoryBlobStore()


@pytest.fixture
def encryption_key() -> str:
    return base64.b64encode(bytes(range(32))).decode("ascii")


def test_round_trip_encrypts_before_blob_store(
    memory_blob_store: MemoryBlobStore, encryption_key: str
) -> None:
    store = EncryptedObjectStore(memory_blob_store, encryption_key)

    result = store.put(
        key="users/u1/resume.pdf",
        plaintext=b"private resume",
        content_type="application/pdf",
    )

    raw = memory_blob_store.objects[result.key]
    assert b"private resume" not in raw.body
    assert len(raw.body) == 12 + len(b"private resume") + 16
    assert raw.content_type == "application/pdf"
    assert raw.metadata == {"encryption": "v1-aes-256-gcm"}
    assert result.key == "users/u1/resume.pdf"
    assert result.content_type == "application/pdf"
    assert result.plaintext_size == len(b"private resume")
    assert result.encryption == "v1-aes-256-gcm"
    assert store.get(key=result.key) == b"private resume"


def test_each_put_uses_a_fresh_twelve_byte_nonce(
    memory_blob_store: MemoryBlobStore, encryption_key: str
) -> None:
    store = EncryptedObjectStore(memory_blob_store, encryption_key)
    store.put(key="users/u1/a", plaintext=b"same", content_type="text/plain")
    store.put(key="users/u1/b", plaintext=b"same", content_type="text/plain")

    first = memory_blob_store.objects["users/u1/a"].body
    second = memory_blob_store.objects["users/u1/b"].body
    assert first[:12] != second[:12]
    assert first != second


def test_ciphertext_tampering_is_rejected(
    memory_blob_store: MemoryBlobStore, encryption_key: str
) -> None:
    store = EncryptedObjectStore(memory_blob_store, encryption_key)
    store.put(key="users/u1/a", plaintext=b"secret", content_type="text/plain")
    raw = memory_blob_store.objects["users/u1/a"]
    raw.body = raw.body[:-1] + bytes([raw.body[-1] ^ 1])

    with pytest.raises(InvalidTag):
        store.get(key="users/u1/a")


def test_wrong_object_key_is_rejected_as_wrong_aad(
    memory_blob_store: MemoryBlobStore, encryption_key: str
) -> None:
    store = EncryptedObjectStore(memory_blob_store, encryption_key)
    store.put(key="users/u1/a", plaintext=b"secret", content_type="text/plain")
    memory_blob_store.objects["users/u1/b"] = memory_blob_store.objects["users/u1/a"]

    with pytest.raises(InvalidTag):
        store.get(key="users/u1/b")


def test_wrong_encryption_key_is_rejected(
    memory_blob_store: MemoryBlobStore, encryption_key: str
) -> None:
    writer = EncryptedObjectStore(memory_blob_store, encryption_key)
    writer.put(key="users/u1/a", plaintext=b"secret", content_type="text/plain")
    different_key = base64.b64encode(bytes(reversed(range(32)))).decode("ascii")
    reader = EncryptedObjectStore(memory_blob_store, different_key)

    with pytest.raises(InvalidTag):
        reader.get(key="users/u1/a")


@pytest.mark.parametrize(
    "bad_key",
    [
        "not-base64!",
        base64.b64encode(b"too short").decode("ascii"),
        base64.b64encode(bytes(33)).decode("ascii"),
    ],
)
def test_encryption_key_must_be_strict_base64_encoded_32_bytes(
    memory_blob_store: MemoryBlobStore, bad_key: str
) -> None:
    with pytest.raises(ValueError, match="32-byte base64"):
        EncryptedObjectStore(memory_blob_store, bad_key)


def test_s3_blob_store_public_bucket_check_uses_head_bucket() -> None:
    calls: list[dict[str, str]] = []

    class S3Client:
        def head_bucket(self, **kwargs: str) -> None:
            calls.append(kwargs)

    blob_store = S3BlobStore(S3Client(), "readiness-bucket")

    blob_store.check_bucket()

    assert calls == [{"Bucket": "readiness-bucket"}]


def test_inspect_accepts_only_expected_encryption_metadata(
    memory_blob_store: MemoryBlobStore, encryption_key: str
) -> None:
    store = EncryptedObjectStore(memory_blob_store, encryption_key)
    store.put(key="users/u1/a", plaintext=b"secret", content_type="text/plain")
    assert store.inspect(key="users/u1/a").encryption == "v1-aes-256-gcm"
    memory_blob_store.objects["users/u1/a"].metadata = {"encryption": "plaintext"}
    with pytest.raises(ValueError, match="encrypted object metadata"):
        store.inspect(key="users/u1/a")
