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
import { App, Badge, Button, Layout, Menu, Space, Tag, Tooltip, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { coreApi } from "../../shared/api/client";
import { useWorkflowStore } from "../../features/workflows/model/store";

const { Header, Sider, Content } = Layout;

function selectedMenuKey(pathname: string): string {
  if (pathname.startsWith("/instances")) return "/instances";
  if (pathname.startsWith("/providers")) return "/providers";
  if (pathname.startsWith("/settings")) return "/settings/workspaces";
  if (pathname === "/templates") return "/templates";
  return "/templates/new";
}

export function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const { modal } = App.useApp();
  const [collapsed, setCollapsed] = useState(false);
  const resetWorkflow = useWorkflowStore((state) => state.resetWorkflow);
  const workflowDirty = useWorkflowStore((state) => state.dirty);
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
      { key: "/templates/new", icon: <ApartmentOutlined />, label: "模板编排" },
      { key: "/templates", icon: <UnorderedListOutlined />, label: "工作流模板" },
      { key: "/instances", icon: <CloudServerOutlined />, label: "工作流实例" },
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
  const openNewWorkflow = () => {
    const createNew = () => {
      resetWorkflow();
      navigate("/templates/new");
    };
    if (!workflowDirty) {
      createNew();
      return;
    }
    modal.confirm({
      title: "放弃未保存的模板修改？",
      content: "创建新模板前，当前画布的未保存内容将被清除。",
      okText: "放弃并新建模板",
      cancelText: "返回",
      okButtonProps: { danger: true },
      onOk: createNew,
    });
  };

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
          onClick={() => navigate("/templates")}
          aria-label="返回工作流模板库"
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
          onClick={({ key }) => {
            if (key === "/templates/new") {
              openNewWorkflow();
              return;
            }
            navigate(key);
          }}
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
            <Button type="primary" icon={<PlusOutlined />} onClick={openNewWorkflow}>
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
