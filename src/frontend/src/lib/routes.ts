/**
 * The app has two pages: the input page `/` and the report `/analyses/:id`. Old routes redirect here.
 * Self-contained so `node --test` can load it.
 */

/** Where an old path goes now, or null when the path is a current route (or unknown: NotFound). */
export function redirectFor(pathname: string): string | null {
  const path = pathname.replace(/\/+$/, "") || "/";
  const live = /^\/live\/([^/]+)$/.exec(path);
  if (live) return `/analyses/${live[1]}`;
  if (/^\/(corpus|findings|trust|method|home)$/.test(path)) return "/";
  if (/^\/papers(\/.*)?$/.test(path)) return "/";
  return null;
}

export const OLD_ROUTES = ["corpus", "findings", "trust", "method", "home", "papers", "papers/*"] as const;
