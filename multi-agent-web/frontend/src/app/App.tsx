import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntApp, ConfigProvider, Spin, theme } from "antd";
import zhCN from "antd/locale/zh_CN";
import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./layout/AppShell";

const WorkflowListPage = lazy(() =>
  import("../features/workflows/pages/WorkflowListPage").then((module) => ({
    default: module.WorkflowListPage,
  })),
);
const WorkflowEditorPage = lazy(() =>
  import("../features/workflows/pages/WorkflowEditorPage").then((module) => ({
    default: module.WorkflowEditorPage,
  })),
);
const RunsPage = lazy(() =>
  import("../features/runs/pages/RunsPage").then((module) => ({ default: module.RunsPage })),
);
const RunDetailPage = lazy(() =>
  import("../features/runs/pages/RunDetailPage").then((module) => ({
    default: module.RunDetailPage,
  })),
);
const ProvidersPage = lazy(() =>
  import("../features/providers/pages/ProvidersPage").then((module) => ({
    default: module.ProvidersPage,
  })),
);
const WorkspacesPage = lazy(() =>
  import("../features/settings/pages/WorkspacesPage").then((module) => ({
    default: module.WorkspacesPage,
  })),
);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 15_000,
      refetchOnWindowFocus: false,
    },
  },
});

export function App() {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: "#4f46e5",
          colorInfo: "#4f46e5",
          colorBgLayout: "#f4f7fb",
          borderRadius: 10,
          borderRadiusLG: 14,
          fontFamily:
            "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        },
        components: {
          Layout: { headerBg: "rgba(255, 255, 255, 0.92)", siderBg: "#101828" },
          Menu: { darkItemBg: "#101828", darkSubMenuItemBg: "#101828" },
          Drawer: { colorBgElevated: "#ffffff" },
        },
      }}
    >
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <Suspense fallback={<div className="route-loading"><Spin size="large" /></div>}>
              <Routes>
                <Route element={<AppShell />}>
                  <Route index element={<Navigate replace to="/workflows/new" />} />
                  <Route path="/workflows" element={<WorkflowListPage />} />
                  <Route path="/workflows/new" element={<WorkflowEditorPage />} />
                  <Route path="/workflows/:workflowId" element={<WorkflowEditorPage />} />
                  <Route path="/runs" element={<RunsPage />} />
                  <Route path="/runs/:runId" element={<RunDetailPage />} />
                  <Route path="/providers" element={<ProvidersPage />} />
                  <Route path="/settings/workspaces" element={<WorkspacesPage />} />
                  <Route path="*" element={<Navigate replace to="/workflows/new" />} />
                </Route>
              </Routes>
            </Suspense>
          </BrowserRouter>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  );
}
