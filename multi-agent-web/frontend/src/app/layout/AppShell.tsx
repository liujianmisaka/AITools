import {
  ApartmentOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlusOutlined,
  UnorderedListOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Badge, Button, Layout, Menu, Space, Tag, Tooltip, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { coreApi } from "../../shared/api/client";

const { Header, Sider, Content } = Layout;

function selectedMenuKey(pathname: string): string {
  if (pathname.startsWith("/runs")) return "/runs";
  if (pathname.startsWith("/providers")) return "/providers";
  if (pathname.startsWith("/settings")) return "/settings/workspaces";
  if (pathname === "/workflows") return "/workflows";
  return "/workflows/new";
}

export function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  const health = useQuery({
    queryKey: ["core-health"],
    queryFn: coreApi.health,
    refetchInterval: 10_000,
  });
  const providers = useQuery({ queryKey: ["providers"], queryFn: coreApi.providers });
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: coreApi.workspaces });

  useEffect(() => {
    const update = () => setCollapsed(window.innerWidth < 1050);
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  const menuItems = useMemo(
    () => [
      { key: "/workflows/new", icon: <ApartmentOutlined />, label: "工作流编排" },
      { key: "/workflows", icon: <UnorderedListOutlined />, label: "工作流" },
      { key: "/runs", icon: <CloudServerOutlined />, label: "执行记录" },
      { key: "/providers", icon: <DatabaseOutlined />, label: "模型目录" },
      { key: "/settings/workspaces", icon: <DatabaseOutlined />, label: "工作区设置" },
    ],
    [],
  );

  const connected = health.data?.status === "ok" && !health.isError;
  const providerCount = providers.data?.filter((item) => item.available !== false).length ?? 0;
  const workspaceIds = Object.keys(workspaces.data ?? {});
  const workspaceCount = workspaceIds.length;
  const workspaceLabel = workspaceCount === 1 ? workspaceIds[0] : `${workspaceCount} 个工作区`;

  return (
    <Layout className="app-layout">
      <Sider
        className="app-sider"
        width={244}
        collapsedWidth={76}
        collapsed={collapsed}
        trigger={null}
      >
        <button
          className="brand-button"
          type="button"
          onClick={() => navigate("/workflows/new")}
          aria-label="返回工作流编排"
        >
          <span className="brand-glyph" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          {!collapsed && (
            <span className="brand-copy">
              <strong>Multi-Agent Flow</strong>
              <small>ORCHESTRATION CONSOLE</small>
            </span>
          )}
        </button>
        <Menu
          className="app-menu"
          theme="dark"
          mode="inline"
          selectedKeys={[selectedMenuKey(location.pathname)]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
        <div className={`sider-status ${collapsed ? "collapsed" : ""}`}>
          <Badge status={connected ? "success" : health.isLoading ? "processing" : "error"} />
          {!collapsed && (
            <div>
              <span>{connected ? "核心服务已连接" : "核心服务不可用"}</span>
              <small>{providerCount} Provider · {workspaceCount} 工作区</small>
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
              <Typography.Text type="secondary">工作空间</Typography.Text>
              <Typography.Text strong>{workspaceLabel || "尚未连接"}</Typography.Text>
            </div>
          </Space>
          <Space size={10}>
            <Tag color={connected ? "success" : "error"} bordered={false}>
              {connected ? "Core Online" : "Core Offline"}
            </Tag>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate("/workflows/new")}>
              新建工作流
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
