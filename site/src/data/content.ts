/**
 * Marketing copy. Kept separate from markup so wording can be reviewed and
 * changed without touching components. Status values are the single source of
 * truth for what is built versus planned: update them as releases land.
 */

export type Status = 'dev' | 'planned' | 'available';

export const statusLabel: Record<Status, string> = {
  // "Built" = implemented and tested on main, usable in the private preview. Not yet a general release.
  available: 'Built',
  dev: 'In development',
  planned: 'Planned',
};

export const hero = {
  headline: 'Your Analyst. Multiplied.',
  sub: 'An open-source AI Analyst designed to understand your business, investigate your data, build solutions, and get the work done.',
  secondaryCta: 'Explore Lyst',
  stack: ['Python', 'DuckDB', 'Open Source', 'Self-Hosted'],
};

export const problem = {
  eyebrow: 'The problem',
  headline: 'Your data isn’t the problem. The work between question and answer is.',
  points: [
    { icon: 'Network', title: 'Fragmented data', text: 'Answers depend on systems that were never designed to agree.' },
    { icon: 'RefreshCcw', title: 'Repetitive queries', text: 'The same pulls and reports, rebuilt by hand every cycle.' },
    { icon: 'Hourglass', title: 'Slow investigations', text: 'A single “why” can consume days of joining, checking and re-checking.' },
    { icon: 'Sheet', title: 'Spreadsheet reconciliation', text: 'Numbers are copied, adjusted and re-tied until nobody trusts them.' },
    { icon: 'LayoutDashboard', title: 'Dashboard upkeep', text: 'Every new question becomes another view to build and maintain.' },
    { icon: 'BookOpen', title: 'Inconsistent definitions', text: 'Revenue, churn and margin mean different things in different rooms.' },
  ],
  closing:
    'The result is that valuable analytical capacity is spent on routine work. Lyst takes a fundamentally different approach: instead of answering in a chat window, it is being built to do the work.',
};

export const pillars = {
  eyebrow: 'Meet your AI Analyst',
  headline: 'Less busywork. More brainpower.',
  lede: 'Lyst follows the same discipline a strong analyst does, and keeps every step reviewable.',
  items: [
    {
      icon: 'Compass',
      title: 'Understand',
      text: 'Lyst studies your metadata, never the rows, to learn each dataset’s grain, keys and quality risks and the business areas involved, then reads every question in that context.',
      detail: ['Grain, keys and relationships', '13 industry packages', 'Your own words for metrics'],
    },
    {
      icon: 'SquareTerminal',
      title: 'Execute',
      text: 'Lyst is designed to conduct analysis, develop code, build dashboards, and complete multi-step workflows.',
      detail: ['SQL and Python', 'Statistical methods', 'Dashboards and reports', 'Resumes after a restart'],
    },
    {
      icon: 'ShieldCheck',
      title: 'Verify',
      text: 'Lyst emphasizes traceable evidence, reproducible calculations, transparent assumptions, and reviewable results.',
      detail: ['Cited queries', 'Reproducible runs', 'Imperfect data flagged', 'Adversarial checks'],
    },
  ],
};

export const capabilities: {
  icon: string;
  title: string;
  text: string;
  items: string[];
  status: Status;
}[] = [
  {
    icon: 'ChartNoAxesCombined',
    title: 'Analyze',
    text: 'Rigorous quantitative work, end to end.',
    items: ['SQL', 'Python', 'Statistics', 'Forecasting with intervals', 'What-if scenarios', 'Monte Carlo ranges', 'Your fiscal calendar'],
    status: 'available',
  },
  {
    icon: 'ScanSearch',
    title: 'Investigate',
    text: 'Find what changed, and why.',
    items: ['Data discovery', 'Authoritative sources, reconciled', 'Change detection', 'Competing hypotheses', 'Root-cause analysis'],
    status: 'available',
  },
  {
    icon: 'Blocks',
    title: 'Build',
    text: 'Versioned deliverables with an owner, flagged when their data changes and repaired by a verified re-run.',
    items: ['Interactive Dash dashboards', 'Datasets built from a sentence, verified', 'PDF briefings', 'PowerPoint decks', 'Excel and Markdown'],
    status: 'available',
  },
  {
    icon: 'Workflow',
    title: 'Automate',
    text: 'Recurring work on calendar schedules that keep local time, run once after downtime, and never double up.',
    items: ['Approved monitors', 'Automatic investigation', 'Work prepared before it runs', 'Autonomy levels with daily budgets', 'Calendar schedules'],
    status: 'available',
  },
  {
    icon: 'Cable',
    title: 'Connect',
    text: 'Databases, APIs and any file, shown before anything loads. Every connection reports its health in plain words and checks schema changes against its contract.',
    items: ['PostgreSQL', 'SQLite', 'REST APIs', 'Spreadsheets, PDFs and Word', 'Files by URL', 'MCP server', 'Warehouses and SaaS'],
    status: 'dev',
  },
  {
    icon: 'Sprout',
    title: 'Evolve',
    text: 'Learns new methods, with an owner’s approval.',
    items: ['Living knowledge', 'Search across data, definitions and saved work', 'Governed skills', 'New skills drafted, tested and approved'],
    status: 'available',
  },
];

export const openSource = {
  eyebrow: 'Open source',
  headline: 'Powerful by design. Open by principle.',
  lede: 'Lyst is built on an open, inspectable stack you can run on your own infrastructure. Commercial systems are optional connections, never requirements.',
  principles: [
    { icon: 'Scale', title: 'Open-source software', text: 'Licensed under the GNU AGPL v3.0.' },
    { icon: 'Code', title: 'Python-native', text: 'Analysis in the language analysts already use.' },
    { icon: 'Database', title: 'DuckDB-powered', text: 'Fast, in-process SQL on local and remote data.' },
    { icon: 'Server', title: 'Self-hostable', text: 'Deploy on a laptop, a server or your cloud.' },
    { icon: 'Cpu', title: 'Model choice, with real costs', text: 'Economy, Balanced or Frontier levels with automatic fallback, or a local model. Cost per call is shown.' },
    { icon: 'Eye', title: 'Transparent execution', text: 'Every query and calculation can be inspected.' },
    { icon: 'Puzzle', title: 'Extensible integrations', text: 'A Python SDK, MCP, an event stream and signed webhooks, plus connectors and skills as plain code.' },
    { icon: 'Unlock', title: 'No mandatory lock-in', text: 'No required proprietary AI or database service.' },
  ],
  attribution:
    'Python is a trademark of the Python Software Foundation. DuckDB is a trademark of the DuckDB Foundation. Streamlit is a trademark of Snowflake Inc. Plotly and Dash are trademarks of Plotly Technologies Inc. Lyst is not affiliated with or endorsed by these organizations. Connected commercial systems remain the products of their owners and are not part of Lyst’s open-source distribution.',
};

export const architecture = [
  {
    layer: 'Your team',
    items: ['Analyst workspace', 'Python SDK', 'MCP', 'Event stream and signed webhooks'],
    tone: 'ink',
  },
  {
    layer: 'Lyst · open source',
    items: ['Planning', 'Execution', 'Verification', 'Knowledge'],
    tone: 'teal',
  },
  {
    layer: 'Engines',
    items: ['DuckDB', 'Python data stack', 'Statistics', 'Plotly Dash'],
    tone: 'ink',
  },
  {
    layer: 'Models · your choice',
    items: ['Economy · Balanced · Frontier', 'Local models', 'Hosted providers (optional)'],
    tone: 'muted',
  },
  {
    layer: 'Your data · optional connectors',
    items: ['Files', 'Warehouses', 'Business apps', 'APIs'],
    tone: 'muted',
  },
] as const;

export const enterprise = {
  eyebrow: 'Enterprise-ready thinking',
  headline: 'Built for serious analytical work.',
  items: [
    { icon: 'ShieldHalf', title: 'Governed agent execution', text: 'Agents act within explicit permissions. Credentials are entered in Settings, encrypted, released only to the services that need them, and scrubbed from every error.', status: 'available' as Status },
    { icon: 'FileCheck', title: 'Evidence-based results', text: 'Any figure traces to its step, query, definition version, dataset version and source, and can be reproduced.', status: 'available' as Status },
    { icon: 'UserCheck', title: 'Human-in-the-loop approval', text: 'Approvals show the risk, the effect and how to undo it. Decisions are recorded and later checked against what happened.', status: 'available' as Status },
    { icon: 'Library', title: 'Data contracts and lineage', text: 'Versioned contracts flag breaking upstream changes with every affected metric, dashboard and workflow, and any metric can explain where it comes from.', status: 'available' as Status },
    { icon: 'KeyRound', title: 'Role-aware access', text: 'Single sign-on, roles, row and column policies and an enforced data policy decide what each person, model and export can see.', status: 'available' as Status },
    { icon: 'History', title: 'Transparent task history', text: 'Every answer opens a “How this ran” breakdown. Discussions, decisions and assignments sit next to the work, backed by an audit log.', status: 'available' as Status },
    { icon: 'Repeat', title: 'Repeatable workflows', text: 'Verified analyses compile into versioned workflows that schedules can rerun.', status: 'available' as Status },
    { icon: 'Building2', title: 'Deployment independence', text: 'Private by default: the interface listens only on your machine until you open it up. Docker packaging, plus encrypted backups that run while Lyst is up and are checked before any restore.', status: 'available' as Status },
  ],
};

export const workflow = {
  eyebrow: 'How Lyst works',
  headline: 'A better way to get analytical work done.',
  steps: [
    { icon: 'MessageSquareText', title: 'Ask or assign', text: 'Ask a question or assign an objective, in plain language.' },
    { icon: 'Map', title: 'Plan', text: 'Lyst discovers context and plans the work. On day one, it proposes your metrics and terms for an admin to accept.' },
    { icon: 'Cog', title: 'Execute and verify', text: 'Agents execute, analyze, and verify each step.' },
    { icon: 'PackageCheck', title: 'Deliver', text: 'Receive usable answers and downloads, then watch the metric or repeat the analysis in one click.' },
  ],
};

export const earlyAccess = {
  eyebrow: 'Stay in touch',
  body: 'Lyst is being developed for people who want to spend less time preparing analysis and more time using it. Leave your email for product previews, release updates, and opportunities to help shape what’s next.',
  roles: [
    'Business analyst',
    'Analytics engineer',
    'Data scientist',
    'Finance or FP&A',
    'Operations',
    'Manager or leader',
    'Engineer or developer',
    'Other',
  ],
  interests: [
    'Financial analysis',
    'Operational analytics',
    'Dashboards and reporting',
    'Data pipelines',
    'Self-hosted AI',
    'Something else',
  ],
  consent:
    'I agree that Lyst may email me product previews and release updates. I can unsubscribe or ask for my data to be deleted at any time.',
};

export const footer = {
  description: 'Lyst is an open-source AI Analyst in development, built to turn business questions into verified insights and completed analytical work.',
  privacy:
    'This site sets no cookies and runs no analytics or third-party scripts. If you sign up for updates, your email and the optional details you choose to share are used only to contact you about Lyst, and you can ask for them to be deleted at any time.',
};

/**
 * Proof points: facts that can be checked in the public repository. Use floors
 * ("180+") for counts that only grow, so the line stays true between updates.
 */
export const proof = [
  { value: '350+', label: 'automated tests, run on every change' },
  { value: '5', label: 'data connectors' },
  { value: 'Every', label: 'finding ships with its evidence' },
  { value: 'AGPL-3.0', label: 'open source, self-hosted' },
];

export const studio = {
  eyebrow: 'From the private preview',
  headline: 'Real output, not a mockup.',
  lede: 'Unedited screenshots from the private preview, on sample data: Home with this month at a glance, a verified answer, a dataset’s trust check, a connected source, an analysis that knows it is out of date, work waiting on a teammate, and a dashboard Studio built from one sentence, “an operating margin dashboard by region, refreshed weekly.”',
  stepsLabel: 'How Studio builds a dashboard',
  steps: [
    { icon: 'MessageSquareText', title: 'One sentence', text: 'Metric, breakdown and refresh cadence, in plain language.' },
    { icon: 'Database', title: 'Governed extracts', text: 'Every number comes from governed metrics, with ratios kept as numerator and denominator.' },
    { icon: 'ShieldCheck', title: 'Verified build', text: 'SQL is verified, a definitions README is written, and the app is render-checked.' },
    { icon: 'CalendarClock', title: 'Approved refresh', text: 'A refresh schedule starts only after an owner approves it, with a check on every run.' },
  ],
};

export const verification = {
  eyebrow: 'Verification',
  headline: 'Every answer is checked before you see it.',
  lede: 'Lyst plans the work as a graph, runs independent steps in parallel, then recomputes high-consequence results a second way and runs adversarial checks before it calls anything verified.',
};
