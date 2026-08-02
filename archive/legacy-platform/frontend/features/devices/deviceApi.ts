import { request } from "../../api";
import type { DeviceListResponse } from "./deviceTypes";

export function listActiveDevices(
  token: string,
): Promise<DeviceListResponse> {
  return request<DeviceListResponse>("/devices", {}, token);
}
