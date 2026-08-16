import { PlusOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Form, Input, InputNumber, Modal, Select, Switch, Table, Tag, Typography } from "antd";
import { useState } from "react";
import { api } from "../../shared/api/client";
import type { JsonObject } from "../../shared/types";
import { PageHeader } from "../../shared/ui/PageHeader";

interface ScheduleForm {
  scheduleId: string;
  name: string;
  enabled: boolean;
  scheduleKind: "cron" | "interval";
  cron: string;
  everySeconds: number;
  templateId: string;
  templateVersion: number;
  workflowInput: string;
}

export function SchedulesPage() {
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm<ScheduleForm>();
  const kind = Form.useWatch("scheduleKind", form);
  const schedules = useQuery({ queryKey: ["schedules"], queryFn: api.listSchedules });
  const create = useMutation({
    mutationFn: (value: ScheduleForm) => {
      let workflowInput: JsonObject;
      try {
        workflowInput = JSON.parse(value.workflowInput || "{}") as JsonObject;
      } catch {
        throw new Error("工作流输入必须是 JSON 对象");
      }
      return api.createSchedule({
        scheduleId: value.scheduleId,
        name: value.name,
        enabled: value.enabled,
        scheduleKind: value.scheduleKind,
        scheduleSpec:
          value.scheduleKind === "cron"
            ? { expressions: [value.cron], timeZone: "Asia/Shanghai" }
            : { everySeconds: value.everySeconds },
        targetKind: "workflow",
        target: {
          templateId: value.templateId,
          templateVersion: value.templateVersion,
          workflowInput,
        },
      });
    },
    onSuccess: async () => {
      message.success("Schedule 已创建并进入同步 Outbox");
      setOpen(false);
      form.resetFields();
      await queryClient.invalidateQueries({ queryKey: ["schedules"] });
    },
    onError: (error) => message.error(error instanceof Error ? error.message : "创建失败"),
  });

  return (
    <div className="page">
      <PageHeader
        eyebrow="Temporal schedules"
        title="定时计划"
        description="Schedule 定义保存在 PostgreSQL，通过版本化 Outbox 同步到 Temporal。"
        actions={<Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>新建计划</Button>}
      />
      <Card className="surface-card">
        <Table
          rowKey="scheduleId"
          dataSource={schedules.data ?? []}
          pagination={false}
          columns={[
            {
              title: "计划",
              render: (_, record) => (
                <div className="table-primary">
                  <Typography.Text strong>{record.name}</Typography.Text>
                  <Typography.Text type="secondary">{record.scheduleId}</Typography.Text>
                </div>
              ),
            },
            { title: "类型", dataIndex: "scheduleKind", width: 120 },
            { title: "目标", render: (_, record) => String(record.target["templateId"] ?? record.targetKind) },
            { title: "修订", dataIndex: "revision", width: 80 },
            {
              title: "状态",
              dataIndex: "enabled",
              width: 100,
              render: (enabled: boolean) => <Tag color={enabled ? "success" : "default"}>{enabled ? "启用" : "暂停"}</Tag>,
            },
          ]}
        />
      </Card>
      <Modal open={open} title="新建 Temporal Schedule" okText="创建" cancelText="取消" confirmLoading={create.isPending} onCancel={() => setOpen(false)} onOk={() => form.submit()}>
        <Form
          form={form}
          layout="vertical"
          initialValues={{ enabled: true, scheduleKind: "cron", cron: "0 1 * * *", everySeconds: 300, templateVersion: 1, workflowInput: "{}" }}
          onFinish={(value) => create.mutate(value)}
        >
          <Form.Item name="scheduleId" label="Schedule ID" rules={[{ required: true }]}><Input placeholder="nightly-review" /></Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="enabled" label="立即启用" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="scheduleKind" label="计划类型"><Select options={[{ value: "cron", label: "Cron" }, { value: "interval", label: "固定间隔" }]} /></Form.Item>
          {kind === "interval" ? (
            <Form.Item name="everySeconds" label="间隔秒数"><InputNumber min={1} /></Form.Item>
          ) : (
            <Form.Item name="cron" label="Cron 表达式"><Input /></Form.Item>
          )}
          <Form.Item name="templateId" label="模板 ID" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="templateVersion" label="模板版本"><InputNumber min={1} /></Form.Item>
          <Form.Item name="workflowInput" label="工作流输入"><Input.TextArea className="schema-editor" autoSize={{ minRows: 6 }} /></Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
