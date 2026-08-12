import {
  ApiOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  InboxOutlined,
  PlusOutlined,
  ReloadOutlined,
  RetweetOutlined,
  SendOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { useEffect, useMemo, useState } from "react";
import { coreApi } from "../../../shared/api/client";
import type {
  EventSourceDescription,
  EventTypeDescription,
  TriggerDeliveryRecord,
  TriggerEventInput,
  TriggerEventRecord,
  TriggerBindingRecord,
} from "../../../shared/types";
import { TriggerEditorFields } from "../components/TriggerEditorFields";
import {
  newTriggerValues,
  parseJsonObject,
  triggerRecordToValues,
  triggerValuesToDefinition,
} from "../model/triggerForm";
import type { TriggerEditorValues } from "../model/triggerForm";

interface EventPublisherValues {
  source_type: string;
  event_type: string;
  event_version: number;
  source_key?: string;
  dedup_key: string;
  payload_text: string;
}

const eventStatusMeta: Record<string, { color: string; label: string }> = {
  received: { color: "processing", label: "处理中" },
  processed: { color: "success", label: "已处理" },
  failed: { color: "error", label: "失败" },
};

const deliveryStatusMeta: Record<string, { color: string; label: string }> = {
  pending: { color: "processing", label: "等待投递" },
  delivered: { color: "success", label: "已创建实例" },
  skipped: { color: "default", label: "已跳过" },
  failed: { color: "error", label: "投递失败" },
};

function sourceLabel(sourceType: string): string {
  if (sourceType === "git_commit") return "Git 分支提交";
  if (sourceType === "manual") return "手动事件";
  return sourceType;
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

function createDedupKey(): string {
  return `manual_${crypto.randomUUID().replaceAll("-", "")}`;
}

function EventPublisherFields({
  form,
  sources,
  eventTypes,
}: {
  form: ReturnType<typeof Form.useForm<EventPublisherValues>>[0];
  sources: EventSourceDescription[];
  eventTypes: EventTypeDescription[];
}) {
  const sourceType = Form.useWatch("source_type", form);
  const eventType = Form.useWatch("event_type", form);
  const allowedEventTypes = useMemo(
    () => eventTypes.filter((item) => item.source_types.includes(sourceType)),
    [eventTypes, sourceType],
  );

  useEffect(() => {
    const selected = allowedEventTypes.find(
      (item) => item.event_type === eventType,
    );
    if (selected) {
      form.setFieldValue("event_version", selected.version);
      return;
    }
    if (allowedEventTypes.length) {
      form.setFieldsValue({
        event_type: allowedEventTypes[0].event_type,
        event_version: allowedEventTypes[0].version,
      });
    }
  }, [allowedEventTypes, eventType, form]);

  return (
    <>
      <div className="two-column-form">
        <Form.Item
          label="事件源"
          name="source_type"
          rules={[{ required: true, message: "请选择事件源" }]}
        >
          <Select
            options={sources.map((source) => ({
              value: source.source_type,
              label: sourceLabel(source.source_type),
            }))}
          />
        </Form.Item>
        <Form.Item
          label="事件类型"
          name="event_type"
          rules={[{ required: true, message: "请选择事件类型" }]}
        >
          <Select
            options={allowedEventTypes.map((item) => ({
              value: item.event_type,
              label: `${item.event_type}@${item.version}`,
            }))}
          />
        </Form.Item>
      </div>
      <Form.Item name="event_version" hidden>
        <InputNumber />
      </Form.Item>
      <Form.Item
        label="去重键"
        name="dedup_key"
        tooltip="同一事件源中的去重键必须唯一；重复提交相同内容不会再次创建实例"
        rules={[{ required: true, message: "请输入去重键" }]}
      >
        <Input addonAfter={<Button type="text" size="small" onClick={() => form.setFieldValue("dedup_key", createDedupKey())}>重新生成</Button>} />
      </Form.Item>
      <Form.Item
        label="来源键（可选）"
        name="source_key"
        tooltip="只有 source_key 相同或未限制来源键的 Trigger Binding 才会匹配"
      >
        <Input placeholder="例如：release-gate" />
      </Form.Item>
      <Form.Item
        label="事件 Payload"
        name="payload_text"
        rules={[{ required: true, message: "请输入 Payload JSON" }]}
      >
        <Input.TextArea
          rows={9}
          spellCheck={false}
          className="json-editor"
          placeholder={'{\n  "release": "v1.0.0"\n}'}
        />
      </Form.Item>
    </>
  );
}

export function TriggersPage() {
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [editing, setEditing] = useState<TriggerBindingRecord | null>(null);
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const [createForm] = Form.useForm<TriggerEditorValues>();
  const [editForm] = Form.useForm<TriggerEditorValues>();
  const [eventForm] = Form.useForm<EventPublisherValues>();

  const sourcesQuery = useQuery({
    queryKey: ["event-source-types"],
    queryFn: coreApi.eventSources,
  });
  const eventTypesQuery = useQuery({
    queryKey: ["event-types"],
    queryFn: coreApi.eventTypes,
  });
  const templatesQuery = useQuery({
    queryKey: ["templates", "trigger-options"],
    queryFn: () => coreApi.listTemplates({ limit: 100 }),
  });
  const workspacesQuery = useQuery({
    queryKey: ["workspaces"],
    queryFn: coreApi.workspaces,
  });
  const triggersQuery = useQuery({
    queryKey: ["triggers"],
    queryFn: () => coreApi.listTriggers(),
    staleTime: 0,
  });
  const eventsQuery = useQuery({
    queryKey: ["trigger-events"],
    queryFn: () => coreApi.listEvents(100),
    staleTime: 0,
    refetchInterval: (query) =>
      query.state.data?.some((event) => event.status === "received")
        ? 1500
        : false,
  });
  const eventDetailQuery = useQuery({
    queryKey: ["trigger-event", selectedEventId],
    queryFn: () => coreApi.getEvent(selectedEventId!),
    enabled: Boolean(selectedEventId),
    staleTime: 0,
  });

  const invalidateTriggers = () =>
    queryClient.invalidateQueries({ queryKey: ["triggers"] });
  const invalidateEvents = () => {
    queryClient.invalidateQueries({ queryKey: ["trigger-events"] });
    if (selectedEventId) {
      queryClient.invalidateQueries({
        queryKey: ["trigger-event", selectedEventId],
      });
    }
  };

  const createMutation = useMutation({
    mutationFn: (values: TriggerEditorValues) =>
      coreApi.createTrigger(triggerValuesToDefinition(values)),
    onSuccess: (record) => {
      invalidateTriggers();
      setCreateOpen(false);
      message.success(`已创建事件触发规则“${record.name}”`);
    },
    onError: (error: Error) => message.error(error.message),
  });
  const updateMutation = useMutation({
    mutationFn: (values: TriggerEditorValues) =>
      coreApi.updateTrigger(values.id, triggerValuesToDefinition(values)),
    onSuccess: (record) => {
      invalidateTriggers();
      queryClient.invalidateQueries({ queryKey: ["scheduled-tasks"] });
      setEditing(null);
      message.success(`已保存事件触发规则“${record.name}”`);
    },
    onError: (error: Error) => message.error(error.message),
  });
  const toggleMutation = useMutation({
    mutationFn: ({
      id,
      enabled,
    }: {
      id: string;
      enabled: boolean;
    }) => coreApi.setTriggerEnabled(id, enabled),
    onSuccess: (record) => {
      invalidateTriggers();
      queryClient.invalidateQueries({ queryKey: ["scheduled-tasks"] });
      message.success(record.enabled ? "触发规则已启用" : "触发规则已停用");
    },
    onError: (error: Error) => message.error(error.message),
  });
  const archiveMutation = useMutation({
    mutationFn: coreApi.archiveTrigger,
    onSuccess: () => {
      invalidateTriggers();
      queryClient.invalidateQueries({ queryKey: ["scheduled-tasks"] });
      message.success("事件触发规则已归档");
    },
    onError: (error: Error) => message.error(error.message),
  });
  const pollMutation = useMutation({
    mutationFn: coreApi.pollTrigger,
    onSuccess: (result) => {
      invalidateTriggers();
      invalidateEvents();
      message.success(
        result.published.length
          ? `轮询完成，发布 ${result.published.length} 个事件`
          : "轮询完成，没有发现新提交",
      );
    },
    onError: (error: Error) => message.error(error.message),
  });
  const publishMutation = useMutation({
    mutationFn: (values: EventPublisherValues) => {
      const event: TriggerEventInput = {
        source_type: values.source_type,
        event_type: values.event_type,
        event_version: values.event_version,
        source_key: values.source_key?.trim() || null,
        dedup_key: values.dedup_key.trim(),
        payload: parseJsonObject(values.payload_text, "事件 Payload"),
      };
      return coreApi.publishEvent(event);
    },
    onSuccess: (event) => {
      invalidateEvents();
      queryClient.invalidateQueries({ queryKey: ["instances"] });
      setPublishOpen(false);
      message.success(
        event.deduplicated ? "事件已存在，返回原处理记录" : "事件已发布并完成准入处理",
      );
    },
    onError: (error: Error) => message.error(error.message),
  });
  const retryMutation = useMutation({
    mutationFn: coreApi.retryEvent,
    onSuccess: () => {
      invalidateEvents();
      queryClient.invalidateQueries({ queryKey: ["instances"] });
      message.success("事件已重新处理");
    },
    onError: (error: Error) => message.error(error.message),
  });

  const sources = sourcesQuery.data ?? [];
  const eventTypes = eventTypesQuery.data ?? [];
  const templates = templatesQuery.data?.items ?? [];
  const workspaces = workspacesQuery.data ?? {};
  const triggers = triggersQuery.data ?? [];
  const events = eventsQuery.data ?? [];
  const sourceDirectory = new Map(
    sources.map((source) => [source.source_type, source]),
  );
  const pushSources = sources.filter((source) => source.supports_push);
  const catalogError =
    sourcesQuery.error
    ?? eventTypesQuery.error
    ?? templatesQuery.error
    ?? workspacesQuery.error;

  const openCreate = () => {
    const initialSource =
      sources.find((source) => source.source_type === "git_commit")
      ?? sources[0];
    createForm.setFieldsValue(newTriggerValues(initialSource?.source_type));
    setCreateOpen(true);
  };
  const openPublish = () => {
    const source = pushSources[0];
    const eventType = eventTypes.find(
      (item) => source && item.source_types.includes(source.source_type),
    );
    eventForm.setFieldsValue({
      source_type: source?.source_type ?? "",
      event_type: eventType?.event_type ?? "",
      event_version: eventType?.version ?? 1,
      source_key: "",
      dedup_key: createDedupKey(),
      payload_text: "{}",
    });
    setPublishOpen(true);
  };
  const openEdit = (record: TriggerBindingRecord) => {
    editForm.setFieldsValue(triggerRecordToValues(record));
    setEditing(record);
  };
  const submitForm = async (
    form: ReturnType<typeof Form.useForm<TriggerEditorValues>>[0],
    mutation: typeof createMutation | typeof updateMutation,
  ) => {
    try {
      mutation.mutate(await form.validateFields());
    } catch {
      // Ant Design renders field validation errors in place.
    }
  };
  const submitEvent = async () => {
    try {
      publishMutation.mutate(await eventForm.validateFields());
    } catch {
      // Ant Design renders field validation errors in place.
    }
  };

  const triggerColumns: TableColumnsType<TriggerBindingRecord> = [
    {
      title: "触发规则",
      key: "binding",
      render: (_, record) => (
        <div className="management-name-cell">
          <Typography.Text strong>{record.name}</Typography.Text>
          <Typography.Text type="secondary" copyable>
            {record.id}
          </Typography.Text>
        </div>
      ),
    },
    {
      title: "事件入口",
      key: "source",
      width: 240,
      render: (_, record) => (
        <Space direction="vertical" size={3}>
          <Space size={5}>
            <Tag color={record.source_type === "git_commit" ? "gold" : "blue"} bordered={false}>
              {sourceLabel(record.source_type)}
            </Tag>
            <Tag bordered={false}>{record.event_type}@{record.event_version}</Tag>
          </Space>
          <Typography.Text type="secondary" className="management-secondary-line">
            {record.source_key ?? "匹配任意来源键"}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "目标模板",
      dataIndex: "template_id",
      width: 210,
      render: (templateId: string) => (
        <Typography.Text copyable className="management-code">
          {templateId}
        </Typography.Text>
      ),
    },
    {
      title: "状态",
      key: "enabled",
      width: 110,
      render: (_, record) => (
        <Switch
          size="small"
          checked={record.enabled}
          loading={
            toggleMutation.isPending
            && toggleMutation.variables?.id === record.id
          }
          checkedChildren="启用"
          unCheckedChildren="停用"
          onClick={(_, event) => event.stopPropagation()}
          onChange={(enabled) =>
            toggleMutation.mutate({ id: record.id, enabled })}
        />
      ),
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      width: 180,
      render: formatTime,
    },
    {
      title: "操作",
      key: "actions",
      width: 145,
      fixed: "right",
      render: (_, record) => {
        const pollable =
          sourceDirectory.get(record.source_type)?.supports_polling ?? false;
        return (
          <Space size={2} onClick={(event) => event.stopPropagation()}>
            {pollable && (
              <Tooltip title="立即轮询一次">
                <Button
                  type="text"
                  icon={<SyncOutlined />}
                  disabled={!record.enabled}
                  loading={
                    pollMutation.isPending
                    && pollMutation.variables === record.id
                  }
                  onClick={() => pollMutation.mutate(record.id)}
                />
              </Tooltip>
            )}
            <Tooltip title="编辑">
              <Button
                type="text"
                icon={<EditOutlined />}
                onClick={() => openEdit(record)}
              />
            </Tooltip>
            <Popconfirm
              title="归档事件触发规则？"
              description="关联的定时轮询会停止，历史事件不会删除。"
              okText="归档"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => archiveMutation.mutate(record.id)}
            >
              <Tooltip title="归档">
                <Button type="text" danger icon={<DeleteOutlined />} />
              </Tooltip>
            </Popconfirm>
          </Space>
        );
      },
    },
  ];

  const eventColumns: TableColumnsType<TriggerEventRecord> = [
    {
      title: "事件",
      key: "event",
      render: (_, record) => (
        <div className="management-name-cell">
          <Typography.Text strong>
            {record.event_type}@{record.event_version}
          </Typography.Text>
          <Typography.Text type="secondary" copyable>
            {record.id}
          </Typography.Text>
        </div>
      ),
    },
    {
      title: "来源",
      key: "source",
      width: 210,
      render: (_, record) => (
        <Space direction="vertical" size={3}>
          <Tag bordered={false}>{sourceLabel(record.source_type)}</Tag>
          <Typography.Text type="secondary" className="management-secondary-line">
            {record.source_key ?? "无来源键"}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "去重键",
      dataIndex: "dedup_key",
      width: 210,
      render: (value: string) => (
        <Typography.Text className="management-code" ellipsis={{ tooltip: value }}>
          {value}
        </Typography.Text>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 105,
      render: (status: string) => {
        const meta = eventStatusMeta[status] ?? {
          color: "default",
          label: status,
        };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "接收时间",
      dataIndex: "received_at",
      width: 180,
      render: formatTime,
    },
    {
      title: "操作",
      key: "actions",
      width: 105,
      fixed: "right",
      render: (_, record) => (
        <Space size={2}>
          <Tooltip title="查看投递详情">
            <Button
              type="text"
              icon={<EyeOutlined />}
              onClick={() => setSelectedEventId(record.id)}
            />
          </Tooltip>
          {record.status === "failed" && (
            <Tooltip title="重新处理">
              <Button
                type="text"
                icon={<RetweetOutlined />}
                loading={
                  retryMutation.isPending
                  && retryMutation.variables === record.id
                }
                onClick={() => retryMutation.mutate(record.id)}
              />
            </Tooltip>
          )}
        </Space>
      ),
    },
  ];

  const deliveryColumns: TableColumnsType<TriggerDeliveryRecord> = [
    {
      title: "Trigger Binding",
      dataIndex: "trigger_binding_id",
      render: (value: string) => (
        <Typography.Text className="management-code" copyable>
          {value}
        </Typography.Text>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 125,
      render: (status: string) => {
        const meta = deliveryStatusMeta[status] ?? {
          color: "default",
          label: status,
        };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "实例 / 原因",
      key: "result",
      render: (_, record) =>
        record.workflow_instance_id
          ?? record.reason
          ?? record.error
          ?? "—",
    },
  ];

  return (
    <div className="page list-page management-page">
      <div className="page-heading">
        <div>
          <div className="page-kicker"><ApiOutlined /> 事件驱动</div>
          <Typography.Title level={2}>事件触发</Typography.Title>
          <Typography.Paragraph type="secondary">
            将已注册的事件源绑定到持久化工作流模板。事件先进入收件箱，再由核心完成契约校验、过滤、输入映射和准入判断。
          </Typography.Paragraph>
        </div>
        <Space wrap>
          <Button
            icon={<ReloadOutlined />}
            loading={triggersQuery.isFetching || eventsQuery.isFetching}
            onClick={() => {
              triggersQuery.refetch();
              eventsQuery.refetch();
            }}
          >
            刷新
          </Button>
          <Button
            icon={<SendOutlined />}
            disabled={!pushSources.length}
            onClick={openPublish}
          >
            发布事件
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={!sources.length || !templates.length}
            onClick={openCreate}
          >
            新建触发规则
          </Button>
        </Space>
      </div>

      {catalogError && (
        <Alert
          className="management-alert"
          type="error"
          showIcon
          message="无法加载事件配置目录"
          description={(catalogError as Error).message}
        />
      )}
      {!catalogError && !templatesQuery.isLoading && !templates.length && (
        <Alert
          className="management-alert"
          type="warning"
          showIcon
          message="请先保存至少一个工作流模板"
          description="Trigger Binding 只能指向已经由核心服务持久化的模板。"
        />
      )}

      <div className="management-summary-grid">
        <Card size="small">
          <span>触发规则</span>
          <strong>{triggers.length}</strong>
          <small>{triggers.filter((item) => item.enabled).length} 个已启用</small>
        </Card>
        <Card size="small">
          <span>事件源</span>
          <strong>{sources.length}</strong>
          <small>{sources.filter((item) => item.supports_polling).length} 个支持轮询</small>
        </Card>
        <Card size="small">
          <span>事件收件箱</span>
          <strong>{events.length}</strong>
          <small>{events.filter((item) => item.status === "failed").length} 个失败事件</small>
        </Card>
      </div>

      <Card
        className="management-card"
        title={
          <Space><ApiOutlined /><span>Trigger Binding</span></Space>
        }
        extra={<Typography.Text type="secondary">事件源 → 工作流模板</Typography.Text>}
      >
        {triggersQuery.isLoading ? (
          <div className="centered-state"><Spin size="large" /></div>
        ) : triggersQuery.isError ? (
          <Alert
            type="error"
            showIcon
            message="无法加载事件触发规则"
            description={(triggersQuery.error as Error).message}
          />
        ) : triggers.length ? (
          <Table<TriggerBindingRecord>
            rowKey="id"
            columns={triggerColumns}
            dataSource={triggers}
            pagination={false}
            scroll={{ x: 1080 }}
            rowClassName="management-row"
            onRow={(record) => ({ onClick: () => openEdit(record) })}
          />
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有事件触发规则"
          >
            <Button
              type="primary"
              icon={<PlusOutlined />}
              disabled={!sources.length || !templates.length}
              onClick={openCreate}
            >
              创建第一条规则
            </Button>
          </Empty>
        )}
      </Card>

      <Card
        className="management-card"
        title={<Space><InboxOutlined /><span>Event Inbox</span></Space>}
        extra={<Typography.Text type="secondary">最近 {events.length} 条事件</Typography.Text>}
      >
        {eventsQuery.isLoading ? (
          <div className="centered-state"><Spin size="large" /></div>
        ) : eventsQuery.isError ? (
          <Alert
            type="error"
            showIcon
            message="无法加载事件收件箱"
            description={(eventsQuery.error as Error).message}
          />
        ) : events.length ? (
          <Table<TriggerEventRecord>
            rowKey="id"
            columns={eventColumns}
            dataSource={events}
            pagination={{ pageSize: 10, hideOnSinglePage: true }}
            scroll={{ x: 900 }}
          />
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="尚未接收到事件"
          />
        )}
      </Card>

      <Modal
        open={createOpen}
        width={760}
        destroyOnHidden
        title={
          <div className="modal-title-block">
            <span>新建事件触发规则</span>
            <small>定义事件来源、目标模板和准入条件</small>
          </div>
        }
        okText="创建规则"
        cancelText="取消"
        confirmLoading={createMutation.isPending}
        onCancel={() => setCreateOpen(false)}
        onOk={() => submitForm(createForm, createMutation)}
      >
        <Form
          form={createForm}
          layout="vertical"
          className="task-create-form trigger-editor-form"
          initialValues={newTriggerValues()}
        >
          <TriggerEditorFields
            form={createForm}
            sources={sources}
            eventTypes={eventTypes}
            templates={templates}
            workspaces={workspaces}
          />
        </Form>
      </Modal>

      <Drawer
        open={Boolean(editing)}
        width={620}
        destroyOnHidden
        className="task-inspector management-drawer"
        title={
          <div className="drawer-title-block">
            <span>编辑事件触发规则</span>
            <small>{editing?.id}</small>
          </div>
        }
        onClose={() => setEditing(null)}
        footer={
          <div className="drawer-footer">
            <Typography.Text type="secondary">
              保存后关联的定时轮询会重新校验
            </Typography.Text>
            <Space>
              <Button onClick={() => setEditing(null)}>取消</Button>
              <Button
                type="primary"
                loading={updateMutation.isPending}
                onClick={() => submitForm(editForm, updateMutation)}
              >
                保存
              </Button>
            </Space>
          </div>
        }
      >
        <Form
          form={editForm}
          layout="vertical"
          className="inspector-form trigger-editor-form"
        >
          <TriggerEditorFields
            editing
            form={editForm}
            sources={sources}
            eventTypes={eventTypes}
            templates={templates}
            workspaces={workspaces}
          />
        </Form>
      </Drawer>

      <Modal
        open={publishOpen}
        width={650}
        destroyOnHidden
        title={
          <div className="modal-title-block">
            <span>发布手动事件</span>
            <small>事件会先写入持久化收件箱，再匹配启用的 Trigger Binding</small>
          </div>
        }
        okText="发布事件"
        cancelText="取消"
        confirmLoading={publishMutation.isPending}
        onCancel={() => setPublishOpen(false)}
        onOk={submitEvent}
      >
        <Form
          form={eventForm}
          layout="vertical"
          className="task-create-form"
        >
          <EventPublisherFields
            form={eventForm}
            sources={pushSources}
            eventTypes={eventTypes}
          />
        </Form>
      </Modal>

      <Drawer
        open={Boolean(selectedEventId)}
        width={680}
        className="task-inspector management-drawer"
        title={
          <div className="drawer-title-block">
            <span>事件投递详情</span>
            <small>{selectedEventId}</small>
          </div>
        }
        onClose={() => setSelectedEventId(null)}
        extra={
          eventDetailQuery.data?.status === "failed" ? (
            <Button
              icon={<RetweetOutlined />}
              loading={retryMutation.isPending}
              onClick={() => retryMutation.mutate(eventDetailQuery.data!.id)}
            >
              重新处理
            </Button>
          ) : undefined
        }
      >
        {eventDetailQuery.isLoading ? (
          <div className="centered-state"><Spin size="large" /></div>
        ) : eventDetailQuery.isError ? (
          <Alert
            type="error"
            showIcon
            message="无法加载事件详情"
            description={(eventDetailQuery.error as Error).message}
          />
        ) : eventDetailQuery.data ? (
          <>
            <Descriptions
              size="small"
              bordered
              column={1}
              items={[
                {
                  key: "event",
                  label: "事件类型",
                  children: `${eventDetailQuery.data.event_type}@${eventDetailQuery.data.event_version}`,
                },
                {
                  key: "source",
                  label: "事件源",
                  children: sourceLabel(eventDetailQuery.data.source_type),
                },
                {
                  key: "source-key",
                  label: "来源键",
                  children: eventDetailQuery.data.source_key ?? "—",
                },
                {
                  key: "dedup",
                  label: "去重键",
                  children: eventDetailQuery.data.dedup_key,
                },
                {
                  key: "received",
                  label: "接收时间",
                  children: formatTime(eventDetailQuery.data.received_at),
                },
                {
                  key: "processed",
                  label: "处理时间",
                  children: formatTime(eventDetailQuery.data.processed_at),
                },
              ]}
            />
            {eventDetailQuery.data.error && (
              <Alert
                className="management-section"
                type="error"
                showIcon
                message="事件处理失败"
                description={eventDetailQuery.data.error}
              />
            )}
            <Typography.Title level={5} className="management-section-title">
              Payload
            </Typography.Title>
            <pre className="management-json-viewer">
              {JSON.stringify(eventDetailQuery.data.payload, null, 2)}
            </pre>
            <Typography.Title level={5} className="management-section-title">
              投递记录
            </Typography.Title>
            <Table<TriggerDeliveryRecord>
              rowKey="id"
              size="small"
              columns={deliveryColumns}
              dataSource={eventDetailQuery.data.deliveries ?? []}
              pagination={false}
              scroll={{ x: 560 }}
              locale={{ emptyText: "没有匹配的 Trigger Binding" }}
            />
          </>
        ) : null}
      </Drawer>
    </div>
  );
}
