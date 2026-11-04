/**
 * Site-wide configuration. Every value that differs between environments or
 * launch stages lives here, read from PUBLIC_* build-time variables.
 * Nothing in this file may ever be secret: it is compiled into static HTML.
 */

export type LaunchMode = 'stealth' | 'public';

const env = import.meta.env;

function clean(value: string | undefined): string {
  return (value ?? '').trim();
}

function launchMode(value: string): LaunchMode {
  return value === 'public' ? 'public' : 'stealth';
}

function httpsUrl(value: string): string {
  if (!value) return '';
  try {
    const url = new URL(value);
    // Allow plain http only for local development endpoints.
    const local = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
    return url.protocol === 'https:' || (url.protocol === 'http:' && local) ? url.toString() : '';
  } catch {
    return '';
  }
}

function email(value: string): string {
  return /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(value) ? value : '';
}

const mode = launchMode(clean(env.PUBLIC_LAUNCH_MODE));

export const site = {
  name: 'Lyst',
  tagline: 'Your Analyst. Multiplied.',
  title: 'LYST — Your Analyst. Multiplied. | Open-Source AI Analyst',
  description:
    'Lyst is an open-source autonomous AI Analyst designed to turn business questions into verified insights, working solutions, and completed analytical work.',
  mode,
  /** Waitlist API endpoint. Empty string means "not configured". */
  waitlistEndpoint: httpsUrl(clean(env.PUBLIC_WAITLIST_ENDPOINT)),
  contactEmail: email(clean(env.PUBLIC_CONTACT_EMAIL)),
  githubUrl: httpsUrl(clean(env.PUBLIC_GITHUB_URL)).replace(/\/$/, ''),
  copyrightHolder: 'The Lyst authors',
  license: 'AGPL-3.0-only',
} as const;

const github = httpsUrl(clean(env.PUBLIC_GITHUB_URL)).replace(/\/$/, '');

/** Stage-dependent labels. Switching PUBLIC_LAUNCH_MODE changes all of them at once. */
export const stage =
  mode === 'public'
    ? {
        badge: 'Open-source release',
        heroNote: 'Now available as open source. Self-host it on your own infrastructure.',
        primaryCta: 'Get Lyst',
        status: 'Released',
        capabilityNote:
          'Each capability carries its current status. Planned items are on the public roadmap and are not yet released.',
        enterpriseNote: 'Items marked as design goals are architectural commitments still being implemented.',
        accessHeadline: 'Meet the next generation of analytical work.',
        primaryHref: github || '#early-access',
      }
    : {
        badge: 'Private Preview',
        heroNote: 'In development. Open-source release planned.',
        primaryCta: 'Get Early Access',
        status: 'In development',
        capabilityNote:
          'Lyst is in active development. These categories describe the product’s design scope, and each carries its current status. Availability is announced with each release.',
        enterpriseNote:
          'These are architectural design goals that guide how Lyst is being built, not certifications or finished features.',
        accessHeadline: 'Meet the next generation of analytical work.',
        primaryHref: '#early-access',
      };

export const nav = [
  { label: 'Product', href: '#product' },
  { label: 'How It Works', href: '#how-it-works' },
  { label: 'Capabilities', href: '#capabilities' },
  { label: 'Open Source', href: '#open-source' },
  { label: 'Early Access', href: '#early-access' },
] as const;
