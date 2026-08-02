import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  applyEvidenceDecisions,
  createProfileVersion,
  downloadResumeAsset,
  fetchProfile,
  fetchProfileVersions,
  fetchResumeAssets,
  reconcileResumeAsset,
  startResumeImport,
  updateLocalSensitiveReference,
  uploadResumeAsset,
} from "../profileApi";
import type { ResumeAssetMetadata } from "../profileTypes";

const asset: ResumeAssetMetadata = {
  id: "a".repeat(36),
  original_filename: "resume.txt",
  content_type: "text/plain",
  plaintext_size: 6,
  encryption_version: "v1-aes-256-gcm",
  status: "ready",
  error_code: null,
  created_at: "2026-07-17T00:00:00Z",
  updated_at: "2026-07-17T00:00:00Z",
};

beforeEach(() => {
  vi.restoreAllMocks();
});

it("uploads a resume as multipart without exposing storage fields", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(asset), {
      status: 201,
      headers: { "Content-Type": "application/json" },
    }),
  );
  await uploadResumeAsset("token", new File(["resume"], "resume.txt", { type: "text/plain" }));
  const [, init] = vi.mocked(fetch).mock.calls[0];
  expect(init?.body).toBeInstanceOf(FormData);
  expect(new Headers(init?.headers).has("Content-Type")).toBe(false);
});

it("uses owner-scoped profile endpoints with only whitelisted bodies", async () => {
  const fetchMock = vi.fn().mockImplementation(() => new Response(JSON.stringify({ assets: [] })));
  vi.stubGlobal("fetch", fetchMock);

  await fetchResumeAssets("token");
  await reconcileResumeAsset("token", "asset/1");
  await startResumeImport("token", "asset-1");
  await fetchProfile("token");
  await applyEvidenceDecisions("token", 3, [{ evidence_id: "e-1", action: "confirm" }]);
  await updateLocalSensitiveReference("token", 3, "contact", "local vault");
  await createProfileVersion("token", 3, "import-1");
  await fetchProfileVersions("token");

  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/api/resume-assets",
    "/api/resume-assets/asset/1/reconcile",
    "/api/resume-imports",
    "/api/profiles",
    "/api/profiles/evidence",
    "/api/profiles/local-sensitive-references",
    "/api/profile-versions",
    "/api/profile-versions",
  ]);
  expect(JSON.parse(fetchMock.mock.calls[4][1].body)).toEqual({
    expected_version: 3,
    decisions: [{ evidence_id: "e-1", action: "confirm" }],
  });
  expect(JSON.parse(fetchMock.mock.calls[5][1].body)).toEqual({
    expected_version: 3, category: "contact", reference: "local vault",
  });
});

it("downloads a resume only after an authorized binary response", async () => {
  const click = vi.fn();
  const createObjectURL = vi.fn().mockReturnValue("blob:resume");
  const revokeObjectURL = vi.fn();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(new Blob(["resume"]), { status: 200 })));
  vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
  vi.spyOn(document, "createElement").mockReturnValue(Object.assign(document.createElement("a"), { click }) as HTMLAnchorElement);

  await downloadResumeAsset("token", "asset-1", "resume.pdf");

  expect(click).toHaveBeenCalledOnce();
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:resume");
});

it("rejects a failed resume download without generating a browser link", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("no", { status: 403 })));

  await expect(downloadResumeAsset("token", "asset-1", "resume.pdf")).rejects.toThrow("download failed");
});
