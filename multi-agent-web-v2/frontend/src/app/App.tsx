import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntApp, ConfigProvider, Spin, theme } from "antd";
import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./layout/AppShell";

const DashboardPage = lazy(() =>
  import("../features/dashboard/DashboardPage").then((module) => ({
    default: module.DashboardPage,
  })),
);
const TemplatesPage = lazy(() =>
  import("../features/workflows/pages/TemplatesPage").then((module) => ({
    default: module.TemplatesPage,
  })),
);
const WorkflowEditorPage = lazy(() =>
  import("../features/workflows/pages/WorkflowEditorPage").then((module) => ({
    default: module.WorkflowEditorPage,
  })),
);
const InstancesPage = lazy(() =>
  import("../features/instances/InstancesPage").then((module) => ({
    default: module.InstancesPage,
  })),
);
const InstanceDetailPage = lazy(() =>
  import("../features/instances/InstanceDetailPage").then((module) => ({
    default: module.InstanceDetailPage,
  })),
);
const ApprovalsPage = lazy(() =>
  import("../features/approvals/ApprovalsPage").then((module) => ({
    default: module.ApprovalsPage,
  })),
);
const TriggersPage = lazy(() =>
  import("../features/triggers/TriggersPage").then((module) => ({
    default: module.TriggersPage,
  })),
);
const SchedulesPage = lazy(() =>
  import("../features/schedules/SchedulesPage").then((module) => ({
    default: module.SchedulesPage,
  })),
);
const CatalogPage = lazy(() =>
  import("../features/catalog/CatalogPage").then((module) => ({
    default: module.CatalogPage,
  })),
);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

export function App() {
  return (
    <ConfigProvider
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: "#4f46e5",
          colorInfo: "#4f46e5",
          colorBgLayout: "#f3f5f9",
          borderRadius: 10,
          fontFamily:
            "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        },
        components: {
          Layout: { siderBg: "#111827", headerBg: "rgba(255,255,255,.86)" },
          Menu: { darkItemBg: "#111827", darkSubMenuItemBg: "#111827" },
        },
      }}
    >
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <Suspense fallback={<div className="route-loading"><Spin size="large" /></div>}>
            <Routes>
              <Route element={<AppShell />}>
                <Route index element={<Navigate to="/dashboard" replace />} />
                <Route path="/dashboard" element={<DashboardPage />} />
                <Route path="/templates" element={<TemplatesPage />} />
                <Route path="/templates/new" element={<WorkflowEditorPage />} />
                <Route path="/templates/:templateId" element={<WorkflowEditorPage />} />
                <Route path="/instances" element={<InstancesPage />} />
                <Route path="/instances/:instanceId" element={<InstanceDetailPage />} />
                <Route path="/approvals" element={<ApprovalsPage />} />
                <Route path="/triggers" element={<TriggersPage />} />
                <Route path="/schedules" element={<SchedulesPage />} />
                <Route path="/catalog" element={<CatalogPage />} />
              </Route>
              <Route path="*" element={<Navigate to="/dashboard" replace />} />
            </Routes>
          </Suspense>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  );
}
