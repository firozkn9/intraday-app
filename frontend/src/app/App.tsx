import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router";
import { ApiError } from "../api/client";
import { CalendarPage, PerformancePage, RiskPage, StrategiesPage } from "../pages/AnalyticsPages";
import { BacktestingPage } from "../pages/BacktestingPage";
import { DashboardPage } from "../pages/DashboardPage";
import { DecisionsPage, TradeDetailPage, TradeHistoryPage } from "../pages/HistoryPages";
import { PositionsPage } from "../pages/PositionsPage";
import { HealthPage, LogsPage } from "../pages/SystemPages";
import { LoginPage } from "../pages/LoginPage";
import { ScannerPage } from "../pages/ScannerPage";
import { SettingsPage } from "../pages/SettingsPage";
import { Layout } from "./Layout";

export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 2000,
        retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      },
    },
  });
}

const queryClient = makeQueryClient();

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<Layout />}>
            <Route index element={<DashboardPage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="scanner" element={<ScannerPage />} />
            <Route path="backtesting" element={<BacktestingPage />} />
            <Route path="positions" element={<PositionsPage />} />
            <Route path="history" element={<TradeHistoryPage />} />
            <Route path="history/:id" element={<TradeDetailPage />} />
            <Route path="decisions" element={<DecisionsPage />} />
            <Route path="strategies" element={<StrategiesPage />} />
            <Route path="risk" element={<RiskPage />} />
            <Route path="calendar" element={<CalendarPage />} />
            <Route path="performance" element={<PerformancePage />} />
            <Route path="health" element={<HealthPage />} />
            <Route path="logs" element={<LogsPage />} />
            <Route path="*" element={<p className="text-muted">Page not found.</p>} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
