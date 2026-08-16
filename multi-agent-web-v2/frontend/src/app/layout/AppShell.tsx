import {
  ApartmentOutlined,
  AuditOutlined,
  ClockCircleOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlusOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Badge, Button, Layout, Menu, Space, Tag, Tooltip, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { api } from "../../shared/api/client";

const { Sider, Header, Content } = Layout;

function selected(pathname: string): string {
  for (const prefix of [
    "/dashboard",
    "/templates",
    "/instances",
    "/approvals",
    "/triggers",
    "/schedules",
    "/catalog",
  ]) {
    if (pathname.startsWith(prefix)) return prefix;
  }
  return "/dashboard";
}

export function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(false);
  const health = useQuery({
    queryKey: ["web-health"],
    queryFn: api.health,
    refetchInterval: 10_000,
  });
  const approvals = useQuery({
    queryKey: ["approvals", true],
    queryFn: () => api.listApprovals(true),
    refetchInterval: 3000,
  });
  const instances = useQuery({
    queryKey: ["instances", "active-shell"],
    queryFn: () => api.listInstances(["pending_start", "running", "waiting"]),
    refetchInterval: 3000,
  });

  useEffect(() => {
    const update = () => setCollapsed(window.innerWidth < 1120);
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  const items = useMemo(
    () => [
      { key: "/dashboard", icon: <DashboardOutlined />, label: "运行概览" },
      { key: "/templates", icon: <ApartmentOutlined />, label: "工作流模板" },
      {
        key: "/instances",
        icon: <ThunderboltOutlined />,
        label: (
          <span className="menu-label">
            工作流实例
            {Boolean(instances.data?.length) && <b>{instances.data?.length}</b>}
          </span>
        ),
      },
      {
        key: "/approvals",
        icon: <AuditOutlined />,
        label: (
          <span className="menu-label">
            人工审批
            {Boolean(approvals.data?.length) && <b>{approvals.data?.length}</b>}
          </span>
        ),
      },
      { key: "/triggers", icon: <ThunderboltOutlined />, label: "事件触发" },
      { key: "/schedules", icon: <ClockCircleOutlined />, label: "定时计划" },
      { key: "/catalog", icon: <DatabaseOutlined />, label: "模型与工作区" },
    ],
    [approvals.data?.length, instances.data?.length],
  );

  const online = health.data?.status === "ok" && !health.isError;

  return (
    <Layout className="app-layout">
      <Sider
        className="app-sider"
        width={252}
        collapsedWidth={76}
        collapsed={collapsed}
        trigger={null}
      >
        <button className="brand" type="button" onClick={() => navigate("/dashboard")}>
          <span className="brand-mark">
            <i />
            <i />
            <i />
          </span>
          {!collapsed && (
            <span className="brand-copy">
              <strong>Multi-Agent</strong>
              <small>CONTROL PLANE V2</small>
            </span>
          )}
        </button>
        <Menu
          className="app-menu"
          mode="inline"
          theme="dark"
          items={items}
          selectedKeys={[selected(location.pathname)]}
          onClick={({ key }) => navigate(key)}
        />
        <div className={`runtime-status ${collapsed ? "is-collapsed" : ""}`}>
          <Badge status={online ? "success" : health.isLoading ? "processing" : "error"} />
          {!collapsed && (
            <div>
              <span>{online ? "Web/BFF 已连接" : "Web/BFF 不可用"}</span>
              <small>
                {health.data?.streamHub?.["subscribers"]?.toString() ?? "0"} 个实时订阅
              </small>
            </div>
          )}
        </div>
      </Sider>
      <Layout>
        <Header className="app-header">
          <Space size={14}>
            <Tooltip title={collapsed ? "展开导航" : "收起导航"}>
              <Button
                type="text"
                icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                onClick={() => setCollapsed((value) => !value)}
              />
            </Tooltip>
            <div className="header-context">
              <Typography.Text type="secondary">本地耐久编排</Typography.Text>
              <Typography.Text strong>Temporal + PostgreSQL</Typography.Text>
            </div>
          </Space>
          <Space>
            <Tag color={online ? "success" : "error"} bordered={false}>
              {online ? "LAN Console Online" : "Console Offline"}
            </Tag>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => navigate("/templates/new")}
            >
              新建模板
            </Button>
          </Space>
        </Header>
        <Content className="app-content">
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
