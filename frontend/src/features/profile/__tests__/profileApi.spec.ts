import { describe, it, expect, vi, beforeEach } from "vitest";
import { uploadResumeAsset } from "../profileApi";
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
