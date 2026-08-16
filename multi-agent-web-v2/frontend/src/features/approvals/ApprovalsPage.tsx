import { CheckOutlined, CloseOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Form, Input, Modal, Space, Table, Typography } from "antd";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../shared/api/client";
import type { ApprovalRecord } from "../../shared/types";
import { PageHeader } from "../../shared/ui/PageHeader";
import { StatusTag } from "../../shared/ui/StatusTag";

export function ApprovalsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [selected, setSelected] = useState<ApprovalRecord | null>(null);
  const [decision, setDecision] = useState<"approved" | "rejected">("approved");
  const [form] = Form.useForm<{ operatorLabel: string; reason: string }>();
  const approvals = useQuery({
    queryKey: ["approvals"],
    queryFn: () => api.listApprovals(false),
    refetchInterval: 2000,
  });
  const decide = useMutation({
    mutationFn: async (value: { operatorLabel: string; reason: string }) => {
      if (!selected) throw new Error("未选择审批");
      return api.decideApproval(
        selected.approvalId,
        decision,
        value.operatorLabel,
        value.reason,
      );
    },
    onSuccess: async () => {
      message.success(decision === "approved" ? "已批准" : "已拒绝");
      setSelected(null);
      form.resetFields();
      await queryClient.invalidateQueries({ queryKey: ["approvals"] });
    },
  });

  return (
    <div className="page">
      <PageHeader
        eyebrow="Human in the loop"
        title="人工审批"
        description="审批由 Temporal Update 同步校验；PostgreSQL 仅保存查询投影和审计记录。"
      />
      <Card className="surface-card">
        <Table
          rowKey="approvalId"
          dataSource={approvals.data ?? []}
          pagination={false}
          columns={[
            {
              title: "审批",
              render: (_, record) => (
                <div className="table-primary">
                  <Typography.Text strong>{record.label}</Typography.Text>
                  <Typography.Text type="secondary">{record.nodeId}</Typography.Text>
                </div>
              ),
            },
            {
              title: "实例",
              dataIndex: "instanceId",
              render: (value: string) => (
                <Button type="link" onClick={() => navigate(`/instances/${value}`)}>
                  {value}
                </Button>
              ),
            },
            { title: "状态", dataIndex: "status", width: 130, render: (v) => <StatusTag status={v} /> },
            {
              title: "请求时间",
              dataIndex: "requestedAt",
              width: 190,
              render: (v: string) => new Date(v).toLocaleString(),
            },
            {
              title: "",
              width: 200,
              render: (_, record) =>
                record.status === "pending" && (
                  <Space>
                    <Button
                      type="primary"
                      icon={<CheckOutlined />}
                      onClick={() => {
                        setDecision("approved");
                        setSelected(record);
                      }}
                    >
                      批准
                    </Button>
                    <Button
                      danger
                      icon={<CloseOutlined />}
                      onClick={() => {
                        setDecision("rejected");
                        setSelected(record);
                      }}
                    >
                      拒绝
                    </Button>
                  </Space>
                ),
            },
          ]}
        />
      </Card>
      <Modal
        open={Boolean(selected)}
        title={`${decision === "approved" ? "批准" : "拒绝"} · ${selected?.label ?? ""}`}
        okText={decision === "approved" ? "确认批准" : "确认拒绝"}
        okButtonProps={{ danger: decision === "rejected" }}
        confirmLoading={decide.isPending}
        onCancel={() => setSelected(null)}
        onOk={() => form.submit()}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={(value) => decide.mutate(value)}
          initialValues={{ operatorLabel: "", reason: "" }}
        >
          <Form.Item name="operatorLabel" label="操作人标签">
            <Input placeholder="可选，仅作为元数据，不用于身份验证" />
          </Form.Item>
          <Form.Item name="reason" label="说明">
            <Input.TextArea autoSize={{ minRows: 4 }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
