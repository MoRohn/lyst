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

export const intro = {
  eyebrow: 'Lyst in 15 seconds',
  headline: 'Meet Lyst.',
  lede: 'A short animated introduction: ask or assign, investigate, build, connect, and get an answer with what to do next.',
  label: 'Animated introduction to Lyst, 15 seconds.',
  description:
    'An animated guide walks through the Lyst story: ask or assign a question, investigate, build and connect, then an answer explaining why operating expenses rose, with a recommended next step.',
  caption:
    'Animated illustration, not a recording of the product. The character, screens and on-screen text are stylized; see the product showcase below for real screenshots.',
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
      detail: ['Cited queries', 'Reproducible runs', 'Checks of the specialty applied', 'Adversarial checks'],
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
    items: ['SQL', 'Sandboxed Python workbench', 'Statistics', 'Backtested forecasts', 'What-if scenarios', 'Monte Carlo ranges', 'Fiscal quarters and years'],
    status: 'available',
  },
  {
    icon: 'ScanSearch',
    title: 'Investigate',
    text: 'Find what changed, and why.',
    items: ['Data discovery', 'Authoritative sources, reconciled', 'Drill-down in conversation', 'Competing hypotheses', 'Root-cause analysis'],
    status: 'available',
  },
  {
    icon: 'Blocks',
    title: 'Build',
    text: 'Versioned deliverables with an owner, flagged when their data changes and repaired by a verified re-run.',
    items: ['Interactive Dash dashboards', 'An immersive viewer for each deliverable', 'Answers published as pages or expiring links', 'Datasets built from a sentence, verified', 'PDF, decks and workbooks, charts included'],
    status: 'available',
  },
  {
    icon: 'Workflow',
    title: 'Automate',
    text: 'Recurring work on calendar schedules that keep local time, run once after downtime, and never double up.',
    items: ['Graph runs that plan, build, check and fix', 'Event rules, tried by dry run first', 'Approved monitors', 'Verified early warnings', 'Work prepared before it runs', 'Autonomy levels with daily budgets'],
    status: 'available',
  },
  {
    icon: 'Cable',
    title: 'Connect',
    text: 'Databases, APIs and any file, shown before anything loads. Every connection reports its health, warns before credentials expire and tests a fix for schema changes.',
    items: ['PostgreSQL', 'SQLite', 'REST APIs', 'Add from an API description', 'Spreadsheets, PDFs and Word', 'Files by URL', 'MCP server', 'Warehouses and SaaS'],
    status: 'dev',
  },
  {
    icon: 'Sprout',
    title: 'Evolve',
    text: 'Learns new methods, with an owner’s approval.',
    items: ['A map of everything Lyst knows', 'Memories flagged when their data changes', 'Search across data, definitions and saved work', 'Rules learned from repeated failures', 'Governed skills'],
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
    { icon: 'Cpu', title: 'Model choice, with real costs', text: 'Economy, Balanced or Frontier levels with measured routing and automatic fallback, or a local model. Cost per call is shown.' },
    { icon: 'Accessibility', title: 'Accessible by default', text: 'One design system, light and dark, with every screen checked for WCAG 2.2 AA.' },
    { icon: 'Puzzle', title: 'Extensible integrations', text: 'A Python SDK, MCP, an event stream and signed webhooks, plus connectors and skills as plain code.' },
    { icon: 'Unlock', title: 'No mandatory lock-in', text: 'No required proprietary AI or database service.' },
  ],
  attribution:
    'Python is a trademark of the Python Software Foundation. DuckDB is a trademark of the DuckDB Foundation. Streamlit is a trademark of Snowflake Inc. Plotly and Dash are trademarks of Plotly Technologies Inc. Lyst is not affiliated with or endorsed by these organizations. Connected commercial systems remain the products of their owners and are not part of Lyst’s open-source distribution.',
};

export const architecture = [
  {
    layer: 'Your team',
    items: ['Analyst workspace', 'Voice', 'Python SDK', 'MCP', 'Event stream and signed webhooks'],
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
    { icon: 'ShieldHalf', title: 'Governed agent execution', text: 'Every object knows what can be done with it and by whom, the server re-checks each action, and anything that changes something asks a person first. Credentials stay encrypted and out of every error.', status: 'available' as Status },
    { icon: 'FileCheck', title: 'Evidence-based results', text: 'Any figure traces to its step, query, definition version, dataset version and source, and can be reproduced.', status: 'available' as Status },
    { icon: 'UserCheck', title: 'Human-in-the-loop approval', text: 'Approvals show the risk, the effect and how to undo it. In a graph run, anything that cannot be undone waits for a person other than its planner.', status: 'available' as Status },
    { icon: 'Library', title: 'Data contracts and lineage', text: 'Versioned contracts flag breaking upstream changes with every affected metric, dashboard and workflow, and any metric can explain where it comes from.', status: 'available' as Status },
    { icon: 'KeyRound', title: 'Role-aware access', text: 'Single sign-on, roles, row and column policies and an enforced data policy decide what each person, model and export can see.', status: 'available' as Status },
    { icon: 'History', title: 'Transparent task history', text: 'Every task has a frozen contract and leaves a receipt for each run. “How this ran” shows the steps, model calls, tokens, queries, time and cost.', status: 'available' as Status },
    { icon: 'Repeat', title: 'Repeatable workflows', text: 'Verified analyses compile into versioned workflows that schedules can rerun.', status: 'available' as Status },
    { icon: 'Building2', title: 'Deployment independence', text: 'Private by default: the interface listens only on your machine until you open it up. Scheduled, encrypted backups, each test-restored automatically, and whole workspaces that move in one sealed file.', status: 'available' as Status },
  ],
};

export const workflow = {
  eyebrow: 'How Lyst works',
  headline: 'A better way to get analytical work done.',
  steps: [
    { icon: 'MessageSquareText', title: 'Ask or assign', text: 'Type or speak in plain words. Lyst replies in conversation, offers next steps you accept with a yes, and turns a vague project into a living spec.' },
    { icon: 'Map', title: 'Plan', text: 'Lyst discovers context and plans the work. On day one, it proposes your metrics and terms for an admin to accept.' },
    { icon: 'Cog', title: 'Execute and verify', text: 'Agents execute, analyze and verify each step, under one honest status from Preparing to Completed or Needs attention.' },
    { icon: 'PackageCheck', title: 'Deliver', text: 'Receive usable answers and downloads, with next steps proposed for your approval: run now, watch or schedule.' },
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
  { value: '850+', label: 'automated tests, fault injection included' },
  { value: '21', label: 'real questions gate every change' },
  { value: '136', label: 'specialist profiles behind one Analyst' },
  { value: 'AGPL-3.0', label: 'open source, self-hosted' },
];

export const studio = {
  eyebrow: 'From the private preview',
  headline: 'Real output, not a mockup.',
  lede: 'Unedited screenshots from the private preview, on sample data: Home with the agent bar and activity board, an answer with its evidence rail, a conversation, the What if explorer, how your datasets connect, data that checks itself, analyses that know when they are out of date, Brain and its work, the knowledge graph, 136 specialisms, the ⌘K palette, and a dashboard Studio built from one sentence.',
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
