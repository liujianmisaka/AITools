import { ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Select, Space, Table, Typography } from "antd";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../shared/api/client";
import { PageHeader } from "../../shared/ui/PageHeader";
import { StatusTag } from "../../shared/ui/StatusTag";

const terminal = new Set(["succeeded", "failed", "cancelled", "attention_required"]);

export function InstancesPage() {
  const navigate = useNavigate();
  const [statuses, setStatuses] = useState<string[]>([]);
  const instances = useQuery({
    queryKey: ["instances", statuses],
    queryFn: () => api.listInstances(statuses),
    refetchOnMount: "always",
    refetchInterval: (query) =>
      (query.state.data ?? []).some((item) => !terminal.has(item.status)) ? 1500 : false,
  });

  return (
    <div className="page">
      <PageHeader
        eyebrow="Durable execution"
        title="工作流实例"
        description="实例状态来自持久化投影；Temporal 运行详情通过固定 workflow ID 关联。"
        actions={
          <Space>
            <Select
              mode="multiple"
              allowClear
              value={statuses}
              onChange={setStatuses}
              placeholder="筛选状态"
              style={{ minWidth: 240 }}
              options={[
                "pending_start",
                "running",
                "waiting",
                "succeeded",
                "failed",
                "cancelled",
                "attention_required",
              ].map((value) => ({ value, label: value }))}
            />
            <Button icon={<ReloadOutlined />} onClick={() => void instances.refetch()}>
              刷新
            </Button>
          </Space>
        }
      />
      <Card className="surface-card">
        <Table
          rowKey="instanceId"
          loading={instances.isLoading}
          dataSource={instances.data ?? []}
          onRow={(record) => ({
            onClick: () => navigate(`/instances/${record.instanceId}`),
          })}
          pagination={false}
          columns={[
            {
              title: "实例",
              render: (_, record) => (
                <div className="table-primary">
                  <Typography.Text strong>{record.templateId}</Typography.Text>
                  <Typography.Text type="secondary">{record.instanceId}</Typography.Text>
                </div>
              ),
            },
            { title: "版本", dataIndex: "templateVersion", width: 90, render: (v) => `v${v}` },
            { title: "状态", dataIndex: "status", width: 150, render: (v) => <StatusTag status={v} /> },
            { title: "投影版本", dataIndex: "projectionVersion", width: 120 },
            {
              title: "更新时间",
              dataIndex: "updatedAt",
              width: 190,
              render: (v: string) => new Date(v).toLocaleString(),
            },
          ]}
        />
      </Card>
    </div>
  );
}
