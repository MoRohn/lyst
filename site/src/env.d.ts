/// <reference types="astro/client" />
interface ImportMetaEnv {
  readonly PUBLIC_SITE_URL?: string;
  readonly PUBLIC_BASE_PATH?: string;
  readonly PUBLIC_LAUNCH_MODE?: string;
  readonly PUBLIC_WAITLIST_ENDPOINT?: string;
  readonly PUBLIC_CONTACT_EMAIL?: string;
  readonly PUBLIC_GITHUB_URL?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
