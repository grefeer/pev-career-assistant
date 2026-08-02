from __future__ import annotations

import base64
import logging
from unittest.mock import MagicMock

import boto3
import botocore.config
import pytest
from botocore.exceptions import ClientError
from cryptography.exceptions import InvalidTag

from backend.app.services.storage import (
    EncryptedObjectStore,
    S3BlobStore,
    create_encrypted_object_store,
)


def _client_error(code: str, operation: str = "HeadBucket") -> ClientError:
    """Build a real botocore ClientError with the given error code."""
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, operation)


@pytest.fixture
def encryption_key() -> str:
    return base64.b64encode(bytes(range(32))).decode("ascii")


# ---------------------------------------------------------------------------
# S3BlobStore: put_bytes / get_bytes / delete / head
# ---------------------------------------------------------------------------


def test_s3_blob_store_put_bytes_delegates_to_client() -> None:
    client = MagicMock()
    store = S3BlobStore(client, "bucket")

    store.put_bytes(
        key="obj/1",
        body=b"hello",
        content_type="text/plain",
        metadata={"encryption": "v1-aes-256-gcm"},
    )

    client.put_object.assert_called_once_with(
        Bucket="bucket",
        Key="obj/1",
        Body=b"hello",
        ContentType="text/plain",
        Metadata={"encryption": "v1-aes-256-gcm"},
    )


def test_s3_blob_store_get_bytes_reads_body_stream() -> None:
    client = MagicMock()
    body = MagicMock()
    body.read.return_value = b"payload"
    client.get_object.return_value = {"Body": body}
    store = S3BlobStore(client, "bucket")

    assert store.get_bytes(key="obj/1") == b"payload"
    client.get_object.assert_called_once_with(Bucket="bucket", Key="obj/1")


def test_s3_blob_store_delete_calls_delete_object() -> None:
    client = MagicMock()
    store = S3BlobStore(client, "bucket")

    store.delete(key="obj/1")

    client.delete_object.assert_called_once_with(Bucket="bucket", Key="obj/1")


def test_s3_blob_store_head_returns_head_object_mapping() -> None:
    client = MagicMock()
    client.head_object.return_value = {"ContentType": "text/plain", "Metadata": {}}
    store = S3BlobStore(client, "bucket")

    assert store.head(key="obj/1") == {"ContentType": "text/plain", "Metadata": {}}
    client.head_object.assert_called_once_with(Bucket="bucket", Key="obj/1")


# ---------------------------------------------------------------------------
# S3BlobStore.ensure_bucket
# ---------------------------------------------------------------------------


def test_ensure_bucket_creates_when_missing_in_us_east_1() -> None:
    client = MagicMock()
    client.head_bucket.side_effect = _client_error("404")
    store = S3BlobStore(client, "bucket", region="us-east-1")

    store.ensure_bucket()

    client.head_bucket.assert_called_once_with(Bucket="bucket")
    client.create_bucket.assert_called_once_with(Bucket="bucket")


def test_ensure_bucket_creates_with_location_constraint_outside_us_east_1() -> None:
    client = MagicMock()
    client.head_bucket.side_effect = _client_error("NoSuchBucket")
    store = S3BlobStore(client, "bucket", region="eu-west-1")

    store.ensure_bucket()

    client.create_bucket.assert_called_once_with(
        Bucket="bucket",
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
    )


def test_ensure_bucket_accepts_not_found_code() -> None:
    client = MagicMock()
    client.head_bucket.side_effect = _client_error("NotFound")
    store = S3BlobStore(client, "bucket")

    store.ensure_bucket()

    client.create_bucket.assert_called_once_with(Bucket="bucket")


def test_ensure_bucket_reraises_non_missing_head_error() -> None:
    client = MagicMock()
    client.head_bucket.side_effect = _client_error("AccessDenied")
    store = S3BlobStore(client, "bucket")

    with pytest.raises(ClientError):
        store.ensure_bucket()

    client.create_bucket.assert_not_called()


@pytest.mark.parametrize(
    "code", ["BucketAlreadyOwnedByYou", "BucketAlreadyExists"]
)
def test_ensure_bucket_recovers_from_already_exists_create_errors(code: str) -> None:
    client = MagicMock()
    # First head_bucket raises (bucket missing); the recovery head_bucket
    # after create_bucket raises BucketAlready* must succeed.
    client.head_bucket.side_effect = [_client_error("404"), None]
    client.create_bucket.side_effect = _client_error(code, "CreateBucket")
    store = S3BlobStore(client, "bucket")

    store.ensure_bucket()

    # head_bucket called twice: once for the initial check, once after create.
    assert client.head_bucket.call_count == 2
    client.head_bucket.assert_called_with(Bucket="bucket")


def test_ensure_bucket_reraises_unexpected_create_error() -> None:
    client = MagicMock()
    client.head_bucket.side_effect = _client_error("404")
    client.create_bucket.side_effect = _client_error("InternalError", "CreateBucket")
    store = S3BlobStore(client, "bucket")

    with pytest.raises(ClientError):
        store.ensure_bucket()

    # Only the initial head_bucket; no recovery head_bucket call.
    client.head_bucket.assert_called_once_with(Bucket="bucket")


# ---------------------------------------------------------------------------
# EncryptedObjectStore: put exception / get InvalidTag / delete paths
# ---------------------------------------------------------------------------


def test_encrypted_put_logs_and_reraises_blob_store_failure(
    encryption_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    blob_store = MagicMock()
    blob_store.put_bytes.side_effect = RuntimeError("disk full")
    store = EncryptedObjectStore(blob_store, encryption_key)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="disk full"):
            store.put(key="k", plaintext=b"secret", content_type="text/plain")

    assert any(
        "encrypted object write failed" in record.message
        for record in caplog.records
    )


def test_encrypted_get_raises_invalid_tag_when_nonce_truncated(
    encryption_key: str,
) -> None:
    blob_store = MagicMock()
    blob_store.get_bytes.return_value = b"short"  # < NONCE_SIZE (12) bytes
    store = EncryptedObjectStore(blob_store, encryption_key)

    with pytest.raises(InvalidTag):
        store.get(key="k")


def test_encrypted_delete_delegates_to_blob_store(encryption_key: str) -> None:
    blob_store = MagicMock()
    store = EncryptedObjectStore(blob_store, encryption_key)

    store.delete("k")

    blob_store.delete.assert_called_once_with(key="k")


def test_encrypted_delete_swallows_file_not_found(encryption_key: str) -> None:
    blob_store = MagicMock()
    blob_store.delete.side_effect = FileNotFoundError("gone")
    store = EncryptedObjectStore(blob_store, encryption_key)

    store.delete("k")  # must not raise


# ---------------------------------------------------------------------------
# create_encrypted_object_store: boto3 client construction (client=None)
# ---------------------------------------------------------------------------


def test_factory_constructs_boto3_client_when_none(
    encryption_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = MagicMock()  # head_bucket returns a MagicMock, no raise
    boto3_client_spy = MagicMock(return_value=fake_client)
    config_spy = MagicMock()
    monkeypatch.setattr(boto3, "client", boto3_client_spy)
    monkeypatch.setattr(botocore.config, "Config", config_spy)

    class Settings:
        object_store_endpoint = "http://minio:9000"
        object_store_region = "us-east-1"
        object_store_access_key = "ak"
        object_store_secret_key = "sk"
        object_store_bucket = "test-bucket"
        object_encryption_key = encryption_key
        readiness_timeout_seconds = 5

    store = create_encrypted_object_store(Settings())

    assert isinstance(store, EncryptedObjectStore)
    boto3_client_spy.assert_called_once_with(
        "s3",
        endpoint_url="http://minio:9000",
        region_name="us-east-1",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        config=config_spy.return_value,
    )
    config_spy.assert_called_once_with(
        connect_timeout=5,
        read_timeout=5,
        retries={"total_max_attempts": 2, "mode": "standard"},
    )
    fake_client.head_bucket.assert_called_once_with(Bucket="test-bucket")
