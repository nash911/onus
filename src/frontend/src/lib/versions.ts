/** True when a report's pipeline fingerprint is known and differs from the app's current one (/api/meta). */
export function isOlderVersion(fingerprint: string | null | undefined, current: string | null | undefined): boolean {
  return !!fingerprint && !!current && fingerprint !== current;
}
