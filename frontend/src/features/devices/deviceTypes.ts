export type DevicePlatform =
  | "android"
  | "ios"
  | "windows"
  | "macos"
  | "linux";

export type DeviceStatus = "active" | "inactive" | "revoked";

export interface DeviceSummary {
  id: string;
  name: string;
  platform: DevicePlatform;
  status: DeviceStatus;
  version: string | null;
  paired_at: string;
  last_seen_at: string | null;
  expires_at: string;
  credential_rotated_at: string | null;
  online: boolean;
}

export interface DeviceListResponse {
  devices: DeviceSummary[];
}
