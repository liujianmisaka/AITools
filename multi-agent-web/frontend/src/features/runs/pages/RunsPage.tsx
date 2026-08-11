import {
  CloudServerOutlined,
  ReloadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import { useInfiniteQuery } from "@tanstack/react-query";
import {
  Button,
  Card,
  Empty,
  Input,
  Progress,
  Result,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { coreApi } from "../../../shared/api/client";
import type { WorkflowInstanceSummary } from "../../../shared/types";

const terminalStatuses = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

const statusMeta: Record<string, { color: string; label: string }> = {
  queued: { color: "geekblue", label: "队列中" },
  running: { color: "processing", label: "执行中" },
  succeeded: { color: "success", label: "成功" },
  failed: { color: "error", label: "失败" },
  cancelled: { color: "default", label: "已取消" },
  interrupted: { color: "warning", label: "已中断" },
};

export function InstancesPage() {
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const instancesQuery = useInfiniteQuery({
    queryKey: ["instances"],
    queryFn: ({ pageParam }) =>
      coreApi.listInstances({ limit: 50, cursor: pageParam || undefined }),
    initialPageParam: "",
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 0,
    refetchOnMount: "always",
    refetchInterval: (query) => {
      const pages = query.state.data?.pages ?? [];
      const hasActive = pages.some((page) =>
        page.items.some((instance) => !terminalStatuses.has(instance.status)),
      );
      return hasActive ? 1000 : false;
    },
  });

  const instances = instancesQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const visibleInstances = useMemo(() => {
    const value = search.trim().toLowerCase();
    if (!value) return instances;
    return instances.filter((instance) =>
      [instance.id, instance.name, instance.template_id ?? ""]
        .some((field) => field.toLowerCase().includes(value)),
    );
  }, [instances, search]);
  const activeCount = instances.filter(
    (instance) => !terminalStatuses.has(instance.status),
  ).length;

  const columns = [
    {
      title: "实例",
      key: "instance",
      render: (_: unknown, instance: WorkflowInstanceSummary) => (
        <div className="instance-name-cell">
          <Typography.Text strong>{instance.name}</Typography.Text>
          <Typography.Text
            type="secondary"
            copyable
            onClick={(event) => event.stopPropagation()}
          >
            {instance.id}
          </Typography.Text>
        </div>
      ),
    },
    {
      title: "来源",
      key: "source",
      width: 180,
      render: (_: unknown, instance: WorkflowInstanceSummary) =>
        instance.source === "template" ? (
          <Space direction="vertical" size={2}>
            <Tag color="blue" bordered={false}>模板 v{instance.template_version}</Tag>
            <Typography.Text type="secondary" className="instance-template-id">
              {instance.template_id}
            </Typography.Text>
          </Space>
        ) : (
          <Tag bordered={false}>临时编排</Tag>
        ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 120,
      render: (status: string) => {
        const meta = statusMeta[status] ?? { color: "default", label: status };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "进度",
      key: "progress",
      width: 190,
      render: (_: unknown, instance: WorkflowInstanceSummary) => {
        const percent = instance.task_count
          ? Math.round((instance.completed_task_count / instance.task_count) * 100)
          : 0;
        return (
          <div className="instance-progress-cell">
            <Progress
              percent={percent}
              size="small"
              status={instance.status === "failed" ? "exception" : undefined}
              showInfo={false}
            />
            <span>{instance.completed_task_count}/{instance.task_count}</span>
          </div>
        );
      },
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      width: 190,
      render: (value: string) => new Date(value).toLocaleString("zh-CN"),
    },
  ];

  return (
    <div className="page list-page instance-list-page">
      <div className="page-heading">
        <div>
          <div className="page-kicker"><CloudServerOutlined /> 工作流实例</div>
          <Typography.Title level={2}>实例中心</Typography.Title>
          <Typography.Paragraph type="secondary">
            每次执行都会生成独立实例并保留模板版本快照；运行中的实例会持续刷新。
          </Typography.Paragraph>
        </div>
        <Space>
          {activeCount > 0 && <Tag color="processing">{activeCount} 个正在执行</Tag>}
          <Button
            icon={<ReloadOutlined />}
            loading={instancesQuery.isFetching}
            onClick={() => instancesQuery.refetch()}
          >
            刷新
          </Button>
        </Space>
      </div>

      <Card className="instance-list-card">
        <div className="instance-list-toolbar">
          <Input
            allowClear
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索实例名称、实例 ID 或模板 ID"
            prefix={<SearchOutlined />}
          />
          <Typography.Text type="secondary">
            共 {instances.length} 条已加载记录
          </Typography.Text>
        </div>

        {instancesQuery.isLoading ? (
          <div className="centered-state"><Spin size="large" tip="正在加载工作流实例" /></div>
        ) : instancesQuery.isError && !instances.length ? (
          <Result
            status="error"
            title="无法加载工作流实例"
            subTitle={(instancesQuery.error as Error).message}
            extra={<Button onClick={() => instancesQuery.refetch()}>重新加载</Button>}
          />
        ) : visibleInstances.length ? (
          <>
            <Table<WorkflowInstanceSummary>
              rowKey="id"
              columns={columns}
              dataSource={visibleInstances}
              pagination={false}
              scroll={{ x: 940 }}
              onRow={(instance) => ({
                onClick: () => navigate(`/instances/${instance.id}`),
                onKeyDown: (event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    navigate(`/instances/${instance.id}`);
                  }
                },
                tabIndex: 0,
                role: "link",
                "aria-label": `打开工作流实例 ${instance.name}`,
              })}
              rowClassName="instance-row"
            />
            {instancesQuery.hasNextPage && (
              <div className="load-more-row">
                <Button
                  loading={instancesQuery.isFetchingNextPage}
                  onClick={() => instancesQuery.fetchNextPage()}
                >
                  加载更多
                </Button>
              </div>
            )}
          </>
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={search ? "没有匹配的工作流实例" : "还没有工作流实例"}
          />
        )}
      </Card>
    </div>
  );
}
