import { PlusOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Form, Input, InputNumber, Modal, Switch, Table, Tag, Typography } from "antd";
import { useState } from "react";
import { api } from "../../shared/api/client";
import type { JsonObject } from "../../shared/types";
import { PageHeader } from "../../shared/ui/PageHeader";

interface TriggerForm {
  triggerId: string;
  name: string;
  enabled: boolean;
  eventType: string;
  sourcePattern?: string;
  subjectPattern?: string;
  templateId: string;
  templateVersion: number;
  inputBindings: string;
}

export function TriggersPage() {
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm<TriggerForm>();
  const triggers = useQuery({ queryKey: ["triggers"], queryFn: api.listTriggers });
  const create = useMutation({
    mutationFn: (value: TriggerForm) => {
      let inputBindings: JsonObject;
      try {
        inputBindings = JSON.parse(value.inputBindings || "{}") as JsonObject;
      } catch {
        throw new Error("输入映射必须是 JSON 对象");
      }
      return api.createTrigger({ ...value, inputBindings });
    },
    onSuccess: async () => {
      message.success("Trigger 已创建");
      setOpen(false);
      form.resetFields();
      await queryClient.invalidateQueries({ queryKey: ["triggers"] });
    },
    onError: (error) => message.error(error instanceof Error ? error.message : "创建失败"),
  });

  return (
    <div className="page">
      <PageHeader
        eyebrow="CloudEvents router"
        title="事件触发"
        description="外部事件先进入持久化 Inbox，再匹配代码约束下的 Trigger 和事件等待订阅。"
        actions={
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
            新建 Trigger
          </Button>
        }
      />
      <Card className="surface-card">
        <Table
          rowKey="triggerId"
          dataSource={triggers.data ?? []}
          pagination={false}
          columns={[
            {
              title: "Trigger",
              render: (_, record) => (
                <div className="table-primary">
                  <Typography.Text strong>{record.name}</Typography.Text>
                  <Typography.Text type="secondary">{record.triggerId}</Typography.Text>
                </div>
              ),
            },
            { title: "事件类型", dataIndex: "eventType" },
            {
              title: "目标",
              render: (_, record) => `${record.templateId} · v${record.templateVersion}`,
            },
            { title: "修订", dataIndex: "revision", width: 80 },
            {
              title: "状态",
              dataIndex: "enabled",
              width: 100,
              render: (enabled: boolean) => (
                <Tag color={enabled ? "success" : "default"}>{enabled ? "启用" : "停用"}</Tag>
              ),
            },
          ]}
        />
      </Card>
      <Modal
        open={open}
        title="新建事件 Trigger"
        okText="创建"
        cancelText="取消"
        confirmLoading={create.isPending}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            enabled: true,
            eventType: "dev.misaka.webhook.received.v1",
            templateVersion: 1,
            inputBindings: "{}",
          }}
          onFinish={(value) => create.mutate(value)}
        >
          <Form.Item name="triggerId" label="Trigger ID" rules={[{ required: true }]}>
            <Input placeholder="on-build" />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="enabled" label="立即启用" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="eventType" label="CloudEvent type" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="sourcePattern" label="Source 匹配"><Input placeholder="urn:misaka:webhook:*" /></Form.Item>
          <Form.Item name="subjectPattern" label="Subject 匹配"><Input /></Form.Item>
          <Form.Item name="templateId" label="模板 ID" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="templateVersion" label="模板版本" rules={[{ required: true }]}><InputNumber min={1} /></Form.Item>
          <Form.Item name="inputBindings" label="JMESPath 输入映射">
            <Input.TextArea className="schema-editor" autoSize={{ minRows: 6 }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
