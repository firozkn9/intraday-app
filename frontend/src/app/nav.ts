// The 14 pages of SPEC §40 (docs/design/10). `phase` = roadmap phase that delivers the page.

export interface NavItem {
  path: string;
  label: string;
  phase: number;
  mobile?: boolean;
  note?: string;
}

export const NAV: NavItem[] = [
  { path: "/", label: "Dashboard", phase: 10, mobile: true },
  { path: "/calendar", label: "Calendar", phase: 11, mobile: true },
  { path: "/positions", label: "Positions", phase: 10, mobile: true },
  { path: "/history", label: "Trade History", phase: 10 },
  { path: "/decisions", label: "AI Decisions", phase: 10 },
  { path: "/strategies", label: "Strategies", phase: 10 },
  { path: "/risk", label: "Risk", phase: 10 },
  { path: "/scanner", label: "Market Scanner", phase: 3 },
  { path: "/backtesting", label: "Backtesting", phase: 6 },
  { path: "/performance", label: "Performance", phase: 11 },
  { path: "/health", label: "System Health", phase: 12 },
  { path: "/logs", label: "Logs", phase: 12 },
  { path: "/settings", label: "Settings", phase: 1, mobile: true },
];
