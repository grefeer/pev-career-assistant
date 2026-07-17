from __future__ import annotations

import logging
from typing import Any

import httpx

from executor.protocol import (
    PROTOCOL_VERSION,
    ExecutorTaskDetail,
    ExecutorTaskDetailV2,
    ExecutorTaskListResponse,
    ExecutorTaskState,
)
from executor.secrets import SecretStore, SecretStoreUnavailableError


logger = logging.getLogger(__name__)


class ApiUnauthorized(RuntimeError):
    pass


class ApiTaskNotFound(RuntimeError):
    pass


class ApiConflict(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class ApiValidationError(RuntimeError):
    pass


class ApiDependencyUnavailable(RuntimeError):
    pass


class UncertainWriteResult(RuntimeError):
    pass


def _parse_task_detail(raw: dict[str, Any]) -> ExecutorTaskDetail | ExecutorTaskDetailV2:
    """Parse an executor task detail response, dispatching on payload version."""
    payload_version = (
        raw.get("payload", {}).get("protocol_version", PROTOCOL_VERSION)
    )
    if payload_version == "executor.v2":
        return ExecutorTaskDetailV2.model_validate(raw)
    return ExecutorTaskDetail.model_validate(raw)


class ExecutorApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        secret_store: SecretStore | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._secret_store = secret_store
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout, transport=transport
        )

    def _device_token(self) -> str:
        if self._secret_store is None:
            raise SecretStoreUnavailableError("no secret store configured")
        token = self._secret_store.get("device-token")
        if token is None:
            raise SecretStoreUnavailableError("device token not found in secret store")
        return token

    def _task_headers(self, task_id: str, lease: str) -> dict[str, str]:
        return {
            "X-Device-Token": self._device_token(),
            "X-Task-ID": task_id,
            "X-Task-Lease": lease,
        }

    def _auth_headers(self) -> dict[str, str]:
        return {"X-Device-Token": self._device_token()}

    def _translate_error(self, response: httpx.Response) -> None:
        status = response.status_code
        if status == 401:
            raise ApiUnauthorized("device authentication failed")
        if status == 404:
            raise ApiTaskNotFound("task not found")
        if status == 409:
            try:
                payload = response.json()
                detail = payload.get("detail", {})
                error_code = payload.get("error_code") or (
                    detail.get("error_code") if isinstance(detail, dict) else None
                ) or "conflict"
            except Exception:
                error_code = "conflict"
            raise ApiConflict(error_code)
        if status == 422:
            raise ApiValidationError("request validation failed")
        if status == 503:
            raise ApiDependencyUnavailable("backend dependency unavailable")
        response.raise_for_status()

    def _read(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if "X-Device-Token" not in headers:
            headers.update(self._auth_headers())
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.request(method, path, headers=headers, **kwargs)
                if response.status_code >= 400:
                    self._translate_error(response)
                return response
            except httpx.TransportError as error:
                last_error = error
                if attempt < 2:
                    continue
                raise
        raise last_error  # type: ignore[misc]

    def _write(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if "X-Device-Token" not in headers:
            headers.update(self._auth_headers())
        try:
            response = self._client.request(method, path, headers=headers, **kwargs)
            if response.status_code >= 400:
                self._translate_error(response)
            return response
        except httpx.TransportError as error:
            raise UncertainWriteResult("write result is uncertain") from error

    def heartbeat(self, version: str) -> None:
        self._write(
            "POST",
            "/api/devices/heartbeat",
            json={"version": version},
        )

    def list_tasks(self) -> ExecutorTaskListResponse:
        response = self._read("GET", "/api/executor/tasks")
        return ExecutorTaskListResponse.model_validate(response.json())

    def issue_lease(self, task_id: str) -> str:
        response = self._write(
            "POST", "/api/devices/task-lease", json={"task_id": task_id}
        )
        return str(response.json()["lease"])

    def get_task(self, task_id: str, lease: str) -> ExecutorTaskDetail | ExecutorTaskDetailV2:
        response = self._read(
            "GET",
            f"/api/executor/tasks/{task_id}",
            headers=self._task_headers(task_id, lease),
        )
        return _parse_task_detail(response.json())

    def report_progress(
        self,
        *,
        task_id: str,
        lease: str,
        expected_version: int,
        target_status: str,
        page_fingerprint: str,
        page_index: int | None,
        field_counts: dict[str, int],
        reason_code: str | None,
    ) -> ExecutorTaskState:
        body = {
            "protocol_version": PROTOCOL_VERSION,
            "expected_version": expected_version,
            "target_status": target_status,
            "page_fingerprint": page_fingerprint,
            "page_index": page_index,
            "field_counts": field_counts,
            "reason_code": reason_code,
        }
        response = self._write(
            "POST",
            f"/api/executor/tasks/{task_id}/progress",
            headers=self._task_headers(task_id, lease),
            json=body,
        )
        return ExecutorTaskState.model_validate(response.json())

    def report_result(
        self,
        *,
        task_id: str,
        lease: str,
        expected_version: int,
        target_status: str,
        page_fingerprint: str,
        reason_code: str,
    ) -> ExecutorTaskState:
        body = {
            "protocol_version": PROTOCOL_VERSION,
            "expected_version": expected_version,
            "target_status": target_status,
            "page_fingerprint": page_fingerprint,
            "reason_code": reason_code,
        }
        response = self._write(
            "POST",
            f"/api/executor/tasks/{task_id}/result",
            headers=self._task_headers(task_id, lease),
            json=body,
        )
        return ExecutorTaskState.model_validate(response.json())

    def download_attachment(
        self, task_id: str, attachment_id: str, lease: str
    ) -> tuple[bytes, str, str]:
        """Download an encrypted resume attachment for a v2 application task.

        Returns ``(body, content_type, filename)``.

        Raises:
            ApiUnauthorized: If device-token or lease is invalid.
            ApiTaskNotFound: If the task or attachment does not exist.
        """
        response = self._read(
            "GET",
            f"/api/executor/tasks/{task_id}/attachments/{attachment_id}",
            headers=self._task_headers(task_id, lease),
        )
        content_type = response.headers.get("content-type", "application/octet-stream")
        disposition = response.headers.get("content-disposition", "")
        filename = "resume.bin"
        if "filename=" in disposition:
            filename = disposition.split("filename=")[-1].split(";")[0].strip('"')
        return response.content, content_type, filename
