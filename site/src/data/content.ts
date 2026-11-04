/**
 * Marketing copy. Kept separate from markup so wording can be reviewed and
 * changed without touching components. Status values are the single source of
 * truth for what is built versus planned: update them as releases land.
 */

export type Status = 'dev' | 'planned' | 'available';

export const statusLabel: Record<Status, string> = {
  available: 'Available',
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
      text: 'Lyst interprets questions in the context of relevant business knowledge, data, and objectives.',
      detail: ['Business definitions', 'Relevant datasets', 'Objective and scope'],
    },
    {
      icon: 'SquareTerminal',
      title: 'Execute',
      text: 'Lyst is designed to conduct analysis, develop code, build dashboards, and complete multi-step workflows.',
      detail: ['SQL and Python', 'Statistical methods', 'Dashboards and reports'],
    },
    {
      icon: 'ShieldCheck',
      title: 'Verify',
      text: 'Lyst emphasizes traceable evidence, reproducible calculations, transparent assumptions, and reviewable results.',
      detail: ['Cited queries', 'Reproducible runs', 'Stated assumptions'],
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
    items: ['SQL', 'Python', 'Statistics', 'Forecasting', 'Modeling', 'Business intelligence'],
    status: 'dev',
  },
  {
    icon: 'ScanSearch',
    title: 'Investigate',
    text: 'Find what changed, and why.',
    items: ['Data discovery', 'Anomaly detection', 'Root-cause analysis', 'Research'],
    status: 'dev',
  },
  {
    icon: 'Blocks',
    title: 'Build',
    text: 'Deliverables people can use on Monday.',
    items: ['Dashboards', 'Pipelines', 'Reports', 'Applications', 'Analytical tools'],
    status: 'dev',
  },
  {
    icon: 'Workflow',
    title: 'Automate',
    text: 'Recurring work that runs on its own schedule.',
    items: ['Recurring analysis', 'Monitoring', 'Reporting', 'Operational workflows'],
    status: 'planned',
  },
  {
    icon: 'Cable',
    title: 'Connect',
    text: 'Work where your data already lives.',
    items: ['Enterprise databases', 'Applications', 'APIs', 'Documents', 'Business systems'],
    status: 'planned',
  },
  {
    icon: 'Sprout',
    title: 'Evolve',
    text: 'Get better with every validated task.',
    items: ['Organizational knowledge', 'Reusable workflows', 'Validated skills', 'Governed capability expansion'],
    status: 'planned',
  },
];

export const openSource = {
  eyebrow: 'Open source',
  headline: 'Powerful by design. Open by principle.',
  lede: 'Lyst is built on an open, inspectable stack you can run on your own infrastructure. Commercial systems are optional connections, never requirements.',
  principles: [
    { icon: 'Scale', title: 'Open-source software', text: 'Apache-2.0 licensed core.' },
    { icon: 'Code', title: 'Python-native', text: 'Analysis in the language analysts already use.' },
    { icon: 'Database', title: 'DuckDB-powered', text: 'Fast, in-process SQL on local and remote data.' },
    { icon: 'Server', title: 'Self-hostable', text: 'Deploy on a laptop, a server or your cloud.' },
    { icon: 'Cpu', title: 'Local model compatible', text: 'Bring a local model or a hosted provider.' },
    { icon: 'Eye', title: 'Transparent execution', text: 'Every query and calculation can be inspected.' },
    { icon: 'Puzzle', title: 'Extensible integrations', text: 'Add connectors and skills as plain code.' },
    { icon: 'Unlock', title: 'No mandatory lock-in', text: 'No required proprietary AI or database service.' },
  ],
  attribution:
    'Python is a trademark of the Python Software Foundation. DuckDB is a trademark of the DuckDB Foundation. Streamlit is a trademark of Snowflake Inc. Lyst is not affiliated with or endorsed by these organizations. Connected commercial systems remain the products of their owners and are not part of Lyst’s open-source distribution.',
};

export const architecture = [
  {
    layer: 'Your team',
    items: ['Analyst workspace', 'Python API', 'Scheduled runs'],
    tone: 'ink',
  },
  {
    layer: 'Lyst · open source',
    items: ['Planning', 'Execution', 'Verification', 'Knowledge'],
    tone: 'teal',
  },
  {
    layer: 'Engines',
    items: ['DuckDB', 'Python data stack', 'Statistics'],
    tone: 'ink',
  },
  {
    layer: 'Models · your choice',
    items: ['Local models', 'Hosted providers (optional)'],
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
    { icon: 'ShieldHalf', title: 'Governed agent execution', text: 'Agents act within explicit, administrator-defined permissions.' },
    { icon: 'FileCheck', title: 'Evidence-based results', text: 'Findings link to the queries, data and assumptions behind them.' },
    { icon: 'UserCheck', title: 'Human-in-the-loop approval', text: 'Consequential steps wait for a person to approve them.' },
    { icon: 'Library', title: 'Organizational context', text: 'Shared definitions and knowledge inform every task.' },
    { icon: 'KeyRound', title: 'Role-aware access', text: 'What Lyst can see and do follows the user’s role.' },
    { icon: 'History', title: 'Transparent task history', text: 'A complete, reviewable record of what was done and why.' },
    { icon: 'Repeat', title: 'Repeatable workflows', text: 'Validated work can be rerun with the same logic.' },
    { icon: 'Building2', title: 'Deployment independence', text: 'Run it where your policies require, including offline.' },
  ],
};

export const workflow = {
  eyebrow: 'How Lyst works',
  headline: 'A better way to get analytical work done.',
  steps: [
    { icon: 'MessageSquareText', title: 'Ask or assign', text: 'Ask a question or assign an objective, in plain language.' },
    { icon: 'Map', title: 'Plan', text: 'Lyst discovers context and plans the work.' },
    { icon: 'Cog', title: 'Execute and verify', text: 'Agents execute, analyze, and verify each step.' },
    { icon: 'PackageCheck', title: 'Deliver', text: 'Receive usable answers, artifacts, and next steps.' },
  ],
};

export const earlyAccess = {
  eyebrow: 'Early access',
  body: 'Lyst is being developed for people who want to spend less time preparing analysis and more time using it. Join the early-access list for product previews, release updates, and opportunities to help shape what’s next.',
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
    'I agree that Lyst may email me about early access, product previews and release updates. I can unsubscribe or ask for my data to be deleted at any time.',
  partner: {
    title: 'Interested in becoming a design partner?',
    text: 'We are looking for a small number of teams with real analytical workloads to shape Lyst with us: early builds, direct access to the people building it, and influence over the roadmap.',
  },
};

export const footer = {
  description: 'Lyst is an open-source AI Analyst in development, built to turn business questions into verified insights and completed analytical work.',
  privacy:
    'This site sets no cookies and runs no analytics or third-party scripts. If you join the early-access list, your email and the optional details you choose to share are used only to contact you about Lyst, and you can ask for them to be deleted at any time.',
};
