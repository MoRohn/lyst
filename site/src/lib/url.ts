/** Prefix a site-relative path with the configured base path. */
export function withBase(path: string): string {
  const base = import.meta.env.BASE_URL.replace(/\/$/, '');
  if (/^(https?:|mailto:|#)/.test(path)) return path;
  return `${base}/${path.replace(/^\//, '')}`;
}

/** Absolute URL for canonical / social metadata. */
export function absolute(path: string, site: URL | undefined): string {
  const origin = site ? site.toString().replace(/\/$/, '') : '';
  return origin + withBase(path);
}
