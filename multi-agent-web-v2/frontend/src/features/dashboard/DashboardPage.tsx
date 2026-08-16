import {
  ApartmentOutlined,
  AuditOutlined,
  ClockCircleOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Card, Col, Row, Space, Statistic, Table, Typography } from "antd";
import { useNavigate } from "react-router-dom";
import { api } from "../../shared/api/client";
import { PageHeader } from "../../shared/ui/PageHeader";
import { StatusTag } from "../../shared/ui/StatusTag";

export function DashboardPage() {
  const navigate = useNavigate();
  const templates = useQuery({ queryKey: ["templates"], queryFn: api.listTemplates });
  const instances = useQuery({
    queryKey: ["instances"],
    queryFn: () => api.listInstances(),
    refetchInterval: 3000,
  });
  const approvals = useQuery({
    queryKey: ["approvals", true],
    queryFn: () => api.listApprovals(true),
    refetchInterval: 3000,
  });
  const schedules = useQuery({ queryKey: ["schedules"], queryFn: api.listSchedules });
  const active = instances.data?.filter((item) =>
    ["pending_start", "running", "waiting"].includes(item.status),
  ).length ?? 0;

  return (
    <div className="page">
      <PageHeader
        eyebrow="Control plane"
        title="运行概览"
        description="从 PostgreSQL 投影恢复关键状态；页面刷新或切换不会丢失正在执行的实例。"
      />
      <Row gutter={[16, 16]}>
        {[
          { title: "模板", value: templates.data?.length ?? 0, icon: <ApartmentOutlined /> },
          { title: "活动实例", value: active, icon: <ThunderboltOutlined /> },
          { title: "待审批", value: approvals.data?.length ?? 0, icon: <AuditOutlined /> },
          { title: "定时计划", value: schedules.data?.length ?? 0, icon: <ClockCircleOutlined /> },
        ].map((item) => (
          <Col xs={24} sm={12} xl={6} key={item.title}>
            <Card className="metric-card">
              <Space align="start">
                <span className="metric-icon">{item.icon}</span>
                <Statistic title={item.title} value={item.value} />
              </Space>
            </Card>
          </Col>
        ))}
      </Row>
      <Card
        className="surface-card dashboard-table"
        title="最近实例"
        extra={<a onClick={() => navigate("/instances")}>查看全部</a>}
      >
        <Table
          rowKey="instanceId"
          dataSource={(instances.data ?? []).slice(0, 8)}
          pagination={false}
          onRow={(record) => ({
            onClick: () => navigate(`/instances/${record.instanceId}`),
          })}
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
            { title: "状态", dataIndex: "status", width: 140, render: (v) => <StatusTag status={v} /> },
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
