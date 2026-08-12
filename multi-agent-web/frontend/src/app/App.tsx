import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntApp, ConfigProvider, Spin, theme } from "antd";
import zhCN from "antd/locale/zh_CN";
import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./layout/AppShell";

const TemplateListPage = lazy(() =>
  import("../features/workflows/pages/WorkflowListPage").then((module) => ({
    default: module.WorkflowListPage,
  })),
);
const TemplateEditorPage = lazy(() =>
  import("../features/workflows/pages/WorkflowEditorPage").then((module) => ({
    default: module.WorkflowEditorPage,
  })),
);
const InstancesPage = lazy(() =>
  import("../features/runs/pages/RunsPage").then((module) => ({
    default: module.InstancesPage,
  })),
);
const TriggersPage = lazy(() =>
  import("../features/triggers/pages/TriggersPage").then((module) => ({
    default: module.TriggersPage,
  })),
);
const ScheduledTasksPage = lazy(() =>
  import("../features/scheduling/pages/ScheduledTasksPage").then((module) => ({
    default: module.ScheduledTasksPage,
  })),
);
const InstanceDetailPage = lazy(() =>
  import("../features/runs/pages/RunDetailPage").then((module) => ({
    default: module.InstanceDetailPage,
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
                  <Route index element={<Navigate replace to="/templates/new" />} />
                  <Route path="/templates" element={<TemplateListPage />} />
                  <Route path="/templates/new" element={<TemplateEditorPage />} />
                  <Route path="/templates/:templateId" element={<TemplateEditorPage />} />
                  <Route path="/instances" element={<InstancesPage />} />
                  <Route path="/instances/:instanceId" element={<InstanceDetailPage />} />
                  <Route path="/triggers" element={<TriggersPage />} />
                  <Route path="/scheduled-tasks" element={<ScheduledTasksPage />} />
                  <Route path="/providers" element={<ProvidersPage />} />
                  <Route path="/settings/workspaces" element={<WorkspacesPage />} />
                  <Route path="*" element={<Navigate replace to="/templates/new" />} />
                </Route>
              </Routes>
            </Suspense>
          </BrowserRouter>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  );
}
