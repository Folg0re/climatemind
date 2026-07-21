import type { DeviceConfig } from "../types";

/**
 * Determine a human-friendly heating system type from configured devices.
 *
 * Heuristics:
 * - If any device declares `heating_system_type`, use that.
 * - If any device is an `ac`, report `ac`.
 * - If any device is a `trv`, report `radiator`.
 * - Otherwise return empty string.
 */
export function resolveHeatingSystemType(devices?: DeviceConfig[] | null): string {
  if (!devices || devices.length === 0) return "";

  const explicit = devices.find((d) => d.heating_system_type && d.heating_system_type.length > 0);
  if (explicit) return explicit.heating_system_type as string;

  if (devices.some((d) => d.type === "ac")) return "ac";
  if (devices.some((d) => d.type === "trv")) return "radiator";

  return "";
}
