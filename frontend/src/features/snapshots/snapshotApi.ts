import { request } from "../../api";
import type {
  CreateSnapshotRequest,
  CreateTaskResponse,
  SnapshotListResponse,
  SnapshotSummary,
  TaskEligibilityResponse,
} from "./snapshotTypes";

function snapshotIdempotencyKey(): string {
  return `snapshot-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export function listSnapshots(
  token: string,
): Promise<SnapshotListResponse> {
  return request<SnapshotListResponse>(
    "/application-snapshots",
    {},
    token,
  );
}

export function getSnapshot(
  token: string,
  snapshotId: string,
): Promise<SnapshotSummary> {
  return request<SnapshotSummary>(
    `/application-snapshots/${encodeURIComponent(snapshotId)}`,
    {},
    token,
  );
}

export function createSnapshot(
  token: string,
  payload: CreateSnapshotRequest,
): Promise<SnapshotSummary> {
  return request<SnapshotSummary>(
    "/application-snapshots",
    {
      method: "POST",
      body: JSON.stringify(payload),
      headers: {
        "Idempotency-Key": snapshotIdempotencyKey(),
      } as Record<string, string>,
    },
    token,
  );
}

export function createTask(
  token: string,
  snapshotId: string,
  deviceId?: string,
): Promise<CreateTaskResponse> {
  return request<CreateTaskResponse>(
    `/application-snapshots/${encodeURIComponent(snapshotId)}/create-task`,
    {
      method: "POST",
      body: JSON.stringify({ device_id: deviceId || null }),
      headers: {
        "Idempotency-Key": snapshotIdempotencyKey(),
      } as Record<string, string>,
    },
    token,
  );
}

export function getTaskEligibility(
  token: string,
  snapshotId: string,
): Promise<TaskEligibilityResponse> {
  return request<TaskEligibilityResponse>(
    `/application-snapshots/${encodeURIComponent(snapshotId)}/task-eligibility`,
    {},
    token,
  );
}
