// Builds the site variants the browser tests run against:
//   dist-test    stealth mode, waitlist endpoint configured (the real FastAPI service on :8799)
//   dist-closed  public mode, no endpoint (truthful closed state), GitHub URL set
//   dist-sub     project-site base path /lyst/ (https://USER.github.io/lyst/)
import { execFileSync } from 'node:child_process';

const variants = [
  {
    out: 'dist-test',
    env: { PUBLIC_SITE_URL: 'http://localhost:4329', PUBLIC_BASE_PATH: '/', PUBLIC_WAITLIST_ENDPOINT: 'http://localhost:8799' },
  },
  {
    out: 'dist-closed',
    env: {
      PUBLIC_SITE_URL: 'http://localhost:4330',
      PUBLIC_BASE_PATH: '/',
      PUBLIC_LAUNCH_MODE: 'public',
      PUBLIC_GITHUB_URL: 'https://github.com/MoRohn/lyst',
      PUBLIC_WAITLIST_ENDPOINT: '',
    },
  },
  {
    out: 'dist-sub',
    env: { PUBLIC_SITE_URL: 'https://morohn.github.io', PUBLIC_BASE_PATH: '/lyst', PUBLIC_WAITLIST_ENDPOINT: '' },
  },
];

for (const v of variants) {
  console.log(`building ${v.out}`);
  execFileSync('npx', ['astro', 'build', '--outDir', v.out], {
    stdio: ['ignore', 'ignore', 'inherit'],
    env: { ...process.env, PUBLIC_LAUNCH_MODE: 'stealth', PUBLIC_CONTACT_EMAIL: '', PUBLIC_GITHUB_URL: '', ...v.env },
  });
}
