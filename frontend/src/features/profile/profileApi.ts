import { request } from "../../api";
import type {
  ConfirmedProfileVersionDetail,
  ConfirmedProfileVersionSummary,
  ProfileDetail,
  ResumeAssetMetadata,
  ResumeImportDetail,
  EvidenceDecisionPayload,
} from "./profileTypes";

export function uploadResumeAsset(
  token: string,
  file: File,
): Promise<ResumeAssetMetadata> {
  const body = new FormData();
  body.append("file", file);
  return request<ResumeAssetMetadata>("/resume-assets", { method: "POST", body }, token);
}

export function fetchResumeAssets(
  token: string,
): Promise<{ assets: ResumeAssetMetadata[] }> {
  return request<{ assets: ResumeAssetMetadata[] }>("/resume-assets", {}, token);
}

export function reconcileResumeAsset(
  token: string,
  assetId: string,
): Promise<ResumeAssetMetadata> {
  return request<ResumeAssetMetadata>(
    `/resume-assets/${assetId}/reconcile`,
    { method: "POST" },
    token,
  );
}

export function deleteResumeAsset(
  token: string,
  assetId: string,
): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(
    `/resume-assets/${assetId}`,
    { method: "DELETE" },
    token,
  );
}

export function startResumeImport(
  token: string,
  assetId: string,
): Promise<ResumeImportDetail> {
  return request<ResumeImportDetail>(
    "/resume-imports",
    {
      method: "POST",
      body: JSON.stringify({ asset_id: assetId }),
    },
    token,
  );
}

export function fetchProfile(token: string): Promise<ProfileDetail> {
  return request<ProfileDetail>("/profiles", {}, token);
}

export function applyEvidenceDecisions(
  token: string,
  expectedVersion: number,
  decisions: EvidenceDecisionPayload[],
): Promise<{ version: number }> {
  return request<{ version: number }>(
    "/profiles/evidence",
    {
      method: "PATCH",
      body: JSON.stringify({
        expected_version: expectedVersion,
        decisions: decisions.map((d) => ({
          evidence_id: d.evidence_id,
          action: d.action,
          corrected_value: d.corrected_value,
        })),
      }),
    },
    token,
  );
}

export function updateLocalSensitiveReference(
  token: string,
  expectedVersion: number,
  category: string,
  reference: string,
): Promise<{ version: number }> {
  return request<{ version: number }>(
    "/profiles/local-sensitive-references",
    {
      method: "PATCH",
      body: JSON.stringify({
        expected_version: expectedVersion,
        category,
        reference,
      }),
    },
    token,
  );
}

export function createProfileVersion(
  token: string,
  expectedVersion: number,
  resumeImportId: string,
): Promise<ConfirmedProfileVersionDetail> {
  return request<ConfirmedProfileVersionDetail>(
    "/profile-versions",
    {
      method: "POST",
      body: JSON.stringify({
        expected_version: expectedVersion,
        resume_import_id: resumeImportId,
      }),
    },
    token,
  );
}

export function fetchProfileVersions(
  token: string,
): Promise<{ versions: ConfirmedProfileVersionSummary[] }> {
  return request<{ versions: ConfirmedProfileVersionSummary[] }>(
    "/profile-versions",
    {},
    token,
  );
}

export function activateProfileVersion(
  token: string,
  versionId: string,
): Promise<{ active_version_id: string }> {
  return request<{ active_version_id: string }>(
    `/profile-versions/${versionId}/activate`,
    { method: "POST" },
    token,
  );
}

export function downloadResumeAsset(
  token: string,
  assetId: string,
  filename: string,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    fetch(`/api/resume-assets/${assetId}/download`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((response) => {
        if (!response.ok) throw new Error("download failed");
        return response.blob();
      })
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        resolve();
      })
      .catch(reject);
  });
}
