from __future__ import annotations

import base64
import os
import uuid

import boto3
import pytest

from backend.app.services.storage import EncryptedObjectStore, S3BlobStore


def test_encrypted_object_round_trip_against_s3() -> None:
    endpoint = os.getenv("TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("TEST_S3_ENDPOINT is not configured")

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.getenv("TEST_S3_ACCESS_KEY", "minioadmin"),
        aws_secret_access_key=os.getenv("TEST_S3_SECRET_KEY", "minioadmin"),
        region_name="us-east-1",
    )
    bucket = os.getenv("TEST_S3_BUCKET", "career-assistant-storage-test")
    blob_store = S3BlobStore(client, bucket)
    blob_store.ensure_bucket()
    object_key = f"integration/{uuid.uuid4().hex}/resume.txt"
    plaintext = b"integration-only private resume"
    encryption_key = base64.b64encode(bytes(range(32))).decode("ascii")
    store = EncryptedObjectStore(blob_store, encryption_key)

    try:
        stored = store.put(
            key=object_key,
            plaintext=plaintext,
            content_type="text/plain",
        )
        raw = client.get_object(Bucket=bucket, Key=object_key)
        raw_body = raw["Body"].read()

        assert plaintext not in raw_body
        assert raw["ContentType"] == "text/plain"
        assert raw["Metadata"] == {"encryption": "v1-aes-256-gcm"}
        assert stored.plaintext_size == len(plaintext)
        assert store.get(key=object_key) == plaintext
    finally:
        blob_store.delete(key=object_key)
