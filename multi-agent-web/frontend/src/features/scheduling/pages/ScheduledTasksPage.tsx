import {
  ClockCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  HistoryOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
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
  Modal,
  Popconfirm,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { useState } from "react";
import { coreApi } from "../../../shared/api/client";
import type {
  ScheduledTaskRecord,
  ScheduledTaskRunRecord,
  TriggerBindingRecord,
} from "../../../shared/types";
import { ScheduledTaskEditorFields } from "../components/ScheduledTaskEditorFields";
import {
  newScheduledTaskValues,
  scheduledTaskRecordToValues,
  scheduledTaskValuesToDefinition,
} from "../model/scheduledTaskForm";
import type { ScheduledTaskEditorValues } from "../model/scheduledTaskForm";

const runStatusMeta: Record<string, { color: string; label: string }> = {
  running: { color: "processing", label: "执行中" },
  succeeded: { color: "success", label: "成功" },
  failed: { color: "error", label: "失败" },
  interrupted: { color: "warning", label: "已中断" },
};

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

function formatSchedule(record: ScheduledTaskRecord): string {
  const schedule = record.schedule;
  if (record.schedule_type === "cron") {
    return `${String(schedule.expression ?? "")} · ${String(schedule.timezone ?? "UTC")}`;
  }
  if (record.schedule_type === "interval") {
    const parts = [
      [Number(schedule.weeks ?? 0), "周"],
      [Number(schedule.days ?? 0), "天"],
      [Number(schedule.hours ?? 0), "小时"],
      [Number(schedule.minutes ?? 0), "分钟"],
      [Number(schedule.seconds ?? 0), "秒"],
    ]
      .filter(([value]) => Number(value) > 0)
      .map(([value, unit]) => `${value}${unit}`);
    return `${parts.join(" ") || "0秒"} · ${String(schedule.timezone ?? "UTC")}`;
  }
  if (record.schedule_type === "one_time") {
    return String(schedule.run_at ?? "");
  }
  return record.schedule_type;
}

function scheduleTypeLabel(scheduleType: string): string {
  if (scheduleType === "cron") return "Cron";
  if (scheduleType === "interval") return "Interval";
  if (scheduleType === "one_time") return "One-time";
  return scheduleType;
}

export function ScheduledTasksPage() {
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [selected, setSelected] = useState<ScheduledTaskRecord | null>(null);
  const [drawerTab, setDrawerTab] = useState("settings");
  const [createForm] = Form.useForm<ScheduledTaskEditorValues>();
  const [editForm] = Form.useForm<ScheduledTaskEditorValues>();

  const tasksQuery = useQuery({
    queryKey: ["scheduled-tasks"],
    queryFn: () => coreApi.listScheduledTasks(),
    staleTime: 0,
    refetchInterval: (query) =>
      query.state.data?.some((task) => task.last_status === "running")
        ? 1500
        : false,
  });
  const triggersQuery = useQuery({
    queryKey: ["triggers"],
    queryFn: () => coreApi.listTriggers(),
  });
  const scheduleTypesQuery = useQuery({
    queryKey: ["schedule-types"],
    queryFn: coreApi.scheduleTypes,
  });
  const actionTypesQuery = useQuery({
    queryKey: ["scheduled-action-types"],
    queryFn: coreApi.scheduledActionTypes,
  });
  const runsQuery = useQuery({
    queryKey: ["scheduled-task-runs", selected?.id],
    queryFn: () => coreApi.listScheduledTaskRuns(selected!.id, 100),
    enabled: Boolean(selected),
    staleTime: 0,
    refetchInterval: (query) =>
      query.state.data?.some((run) => run.status === "running") ? 1000 : false,
  });

  const invalidateTasks = () =>
    queryClient.invalidateQueries({ queryKey: ["scheduled-tasks"] });
  const invalidateRuns = (taskId: string) =>
    queryClient.invalidateQueries({
      queryKey: ["scheduled-task-runs", taskId],
    });

  const createMutation = useMutation({
    mutationFn: (values: ScheduledTaskEditorValues) =>
      coreApi.createScheduledTask(scheduledTaskValuesToDefinition(values)),
    onSuccess: (record) => {
      invalidateTasks();
      setCreateOpen(false);
      message.success(`已创建定时任务“${record.name}”`);
    },
    onError: (error: Error) => message.error(error.message),
  });
  const updateMutation = useMutation({
    mutationFn: (values: ScheduledTaskEditorValues) =>
      coreApi.updateScheduledTask(
        values.id,
        scheduledTaskValuesToDefinition(values),
      ),
    onSuccess: (record) => {
      invalidateTasks();
      setSelected(record);
      editForm.setFieldsValue(scheduledTaskRecordToValues(record));
      message.success(`已保存定时任务“${record.name}”`);
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
    }) => coreApi.setScheduledTaskEnabled(id, enabled),
    onSuccess: (record) => {
      invalidateTasks();
      if (selected?.id === record.id) {
        setSelected(record);
        editForm.setFieldsValue(scheduledTaskRecordToValues(record));
      }
      message.success(record.enabled ? "定时任务已启用" : "定时任务已停用");
    },
    onError: (error: Error) => message.error(error.message),
  });
  const runMutation = useMutation({
    mutationFn: coreApi.runScheduledTask,
    onSuccess: (run) => {
      invalidateTasks();
      invalidateRuns(run.scheduled_task_id);
      queryClient.invalidateQueries({ queryKey: ["trigger-events"] });
      message.success(
        run.status === "succeeded" ? "定时动作执行成功" : `执行状态：${run.status}`,
      );
    },
    onError: (error: Error) => message.error(error.message),
  });
  const archiveMutation = useMutation({
    mutationFn: coreApi.archiveScheduledTask,
    onSuccess: (record) => {
      invalidateTasks();
      if (selected?.id === record.id) setSelected(null);
      message.success("定时任务已归档");
    },
    onError: (error: Error) => message.error(error.message),
  });

  const tasks = tasksQuery.data ?? [];
  const triggers = triggersQuery.data ?? [];
  const scheduleTypes = scheduleTypesQuery.data ?? [];
  const actionTypes = actionTypesQuery.data ?? [];
  const runs = runsQuery.data ?? [];
  const bindingMap = new Map(
    triggers.map((binding) => [binding.id, binding] as const),
  );
  const eligibleBindings = triggers.filter(
    (binding) => binding.enabled && binding.source_type === "git_commit",
  );
  const catalogError =
    triggersQuery.error
    ?? scheduleTypesQuery.error
    ?? actionTypesQuery.error;

  const openCreate = () => {
    const values = newScheduledTaskValues();
    values.schedule_type = scheduleTypes[0]?.schedule_type ?? "cron";
    values.action_type =
      actionTypes[0]?.action_type ?? "poll_trigger_binding";
    values.binding_id = eligibleBindings[0]?.id ?? "";
    createForm.setFieldsValue(values);
    setCreateOpen(true);
  };
  const openTask = (
    record: ScheduledTaskRecord,
    tab: "settings" | "history" = "settings",
  ) => {
    editForm.setFieldsValue(scheduledTaskRecordToValues(record));
    setSelected(record);
    setDrawerTab(tab);
  };
  const submitCreate = async () => {
    try {
      createMutation.mutate(await createForm.validateFields());
    } catch {
      // Ant Design renders validation errors in the form.
    }
  };
  const submitUpdate = async () => {
    try {
      updateMutation.mutate(await editForm.validateFields());
    } catch {
      // Ant Design renders validation errors in the form.
    }
  };

  const taskColumns: TableColumnsType<ScheduledTaskRecord> = [
    {
      title: "定时任务",
      key: "task",
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
      title: "计划",
      key: "schedule",
      width: 245,
      render: (_, record) => (
        <Space direction="vertical" size={3}>
          <Tag color="purple" bordered={false}>{scheduleTypeLabel(record.schedule_type)}</Tag>
          <Typography.Text className="management-code">
            {formatSchedule(record)}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "轮询目标",
      key: "binding",
      width: 220,
      render: (_, record) => {
        if (record.action_type === "publish_trigger_event") {
          return (
            <Space direction="vertical" size={3}>
              <Typography.Text strong>发布 schedule.tick</Typography.Text>
              <Typography.Text type="secondary" className="management-secondary-line">
                计划合成事件
              </Typography.Text>
            </Space>
          );
        }
        const bindingId = String(record.action.binding_id ?? "");
        return (
          <Space direction="vertical" size={3}>
            <Typography.Text strong>
              {bindingMap.get(bindingId)?.name ?? bindingId}
            </Typography.Text>
            <Typography.Text type="secondary" className="management-secondary-line">
              {bindingId}
            </Typography.Text>
          </Space>
        );
      },
    },
    {
      title: "下次执行",
      dataIndex: "next_run_at",
      width: 180,
      render: formatTime,
    },
    {
      title: "最近状态",
      key: "last_status",
      width: 115,
      render: (_, record) => {
        if (record.scheduler_error) return <Tag color="error">调度异常</Tag>;
        if (!record.last_status) return <Tag>尚未执行</Tag>;
        const meta = runStatusMeta[record.last_status] ?? {
          color: "default",
          label: record.last_status,
        };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "启用",
      key: "enabled",
      width: 85,
      render: (_, record) => (
        <Switch
          size="small"
          checked={record.enabled}
          loading={
            toggleMutation.isPending
            && toggleMutation.variables?.id === record.id
          }
          onClick={(_, event) => event.stopPropagation()}
          onChange={(enabled) =>
            toggleMutation.mutate({ id: record.id, enabled })}
        />
      ),
    },
    {
      title: "操作",
      key: "actions",
      width: 165,
      fixed: "right",
      render: (_, record) => (
        <Space size={2} onClick={(event) => event.stopPropagation()}>
          <Tooltip title="立即运行">
            <Button
              type="text"
              icon={<PlayCircleOutlined />}
              loading={
                runMutation.isPending
                && runMutation.variables === record.id
              }
              onClick={() => runMutation.mutate(record.id)}
            />
          </Tooltip>
          <Tooltip title="编辑">
            <Button
              type="text"
              icon={<EditOutlined />}
              onClick={() => openTask(record)}
            />
          </Tooltip>
          <Tooltip title="运行历史">
            <Button
              type="text"
              icon={<HistoryOutlined />}
              onClick={() => openTask(record, "history")}
            />
          </Tooltip>
          <Popconfirm
            title="归档定时任务？"
            description="计时器会被移除，运行历史仍保留。"
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
      ),
    },
  ];

  const runColumns: TableColumnsType<ScheduledTaskRunRecord> = [
    {
      title: "开始时间",
      dataIndex: "started_at",
      width: 180,
      render: formatTime,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (status: string) => {
        const meta = runStatusMeta[status] ?? {
          color: "default",
          label: status,
        };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "结果",
      key: "result",
      render: (_, record) => {
        const content = record.error
          ?? (record.result ? JSON.stringify(record.result) : "—");
        return (
          <Typography.Text
            type={record.error ? "danger" : undefined}
            ellipsis={{ tooltip: content }}
          >
            {content}
          </Typography.Text>
        );
      },
    },
  ];

  const historyPanel = (
    <>
      <div className="management-drawer-toolbar">
        <Typography.Text type="secondary">
          运行历史由核心服务持久化，服务重启不会丢失。
        </Typography.Text>
        <Button
          icon={<ReloadOutlined />}
          loading={runsQuery.isFetching}
          onClick={() => runsQuery.refetch()}
        >
          刷新
        </Button>
      </div>
      {runsQuery.isLoading ? (
        <div className="centered-state"><Spin size="large" /></div>
      ) : runsQuery.isError ? (
        <Alert
          type="error"
          showIcon
          message="无法加载运行历史"
          description={(runsQuery.error as Error).message}
        />
      ) : (
        <Table<ScheduledTaskRunRecord>
          rowKey="id"
          size="small"
          columns={runColumns}
          dataSource={runs}
          pagination={{ pageSize: 12, hideOnSinglePage: true }}
          scroll={{ x: 560 }}
          locale={{ emptyText: "该定时任务尚未执行" }}
          expandable={{
            expandedRowRender: (record) => (
              <pre className="management-json-viewer compact">
                {JSON.stringify(
                  record.error ? { error: record.error } : record.result,
                  null,
                  2,
                )}
              </pre>
            ),
          }}
        />
      )}
    </>
  );

  return (
    <div className="page list-page management-page">
      <div className="page-heading">
        <div>
          <div className="page-kicker"><ClockCircleOutlined /> 持久化调度</div>
          <Typography.Title level={2}>定时任务</Typography.Title>
          <Typography.Paragraph type="secondary">
            使用核心注册的 Cron 计划周期性轮询事件触发规则。这里只配置时间和动作引用，不介入后续工作流执行细节。
          </Typography.Paragraph>
        </div>
        <Space wrap>
          <Button
            icon={<ReloadOutlined />}
            loading={tasksQuery.isFetching}
            onClick={() => tasksQuery.refetch()}
          >
            刷新
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={
              !eligibleBindings.length
              || !scheduleTypes.length
              || !actionTypes.length
            }
            onClick={openCreate}
          >
            新建定时任务
          </Button>
        </Space>
      </div>

      {catalogError && (
        <Alert
          className="management-alert"
          type="error"
          showIcon
          message="无法加载调度配置目录"
          description={(catalogError as Error).message}
        />
      )}
      {!catalogError && !triggersQuery.isLoading && !eligibleBindings.length && (
        <Alert
          className="management-alert"
          type="warning"
          showIcon
          message="没有可轮询的 Trigger Binding"
          description="请先创建并启用 Git 分支提交事件触发规则，再配置 Cron 定时轮询。"
        />
      )}

      <div className="management-summary-grid">
        <Card size="small">
          <span>定时任务</span>
          <strong>{tasks.length}</strong>
          <small>{tasks.filter((item) => item.enabled).length} 个已启用</small>
        </Card>
        <Card size="small">
          <span>下次待执行</span>
          <strong>{tasks.filter((item) => item.next_run_at).length}</strong>
          <small>由持久化定义恢复</small>
        </Card>
        <Card size="small">
          <span>调度异常</span>
          <strong>{tasks.filter((item) => item.scheduler_error).length}</strong>
          <small>停用的关联规则会暂停计时</small>
        </Card>
      </div>

      <Card
        className="management-card"
        title={<Space><ClockCircleOutlined /><span>Scheduler Registry</span></Space>}
        extra={<Typography.Text type="secondary">Cron → Poll Trigger Binding</Typography.Text>}
      >
        {tasksQuery.isLoading ? (
          <div className="centered-state"><Spin size="large" /></div>
        ) : tasksQuery.isError ? (
          <Alert
            type="error"
            showIcon
            message="无法加载定时任务"
            description={(tasksQuery.error as Error).message}
          />
        ) : tasks.length ? (
          <Table<ScheduledTaskRecord>
            rowKey="id"
            columns={taskColumns}
            dataSource={tasks}
            pagination={false}
            scroll={{ x: 1160 }}
            rowClassName="management-row"
            onRow={(record) => ({ onClick: () => openTask(record) })}
          />
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有持久化定时任务"
          >
            <Button
              type="primary"
              icon={<PlusOutlined />}
              disabled={!eligibleBindings.length}
              onClick={openCreate}
            >
              创建第一条计划
            </Button>
          </Empty>
        )}
      </Card>

      <Modal
        open={createOpen}
        width={720}
        destroyOnHidden
        title={
          <div className="modal-title-block">
            <span>新建定时任务</span>
            <small>配置持久化 Cron 计划与注册动作</small>
          </div>
        }
        okText="创建任务"
        cancelText="取消"
        confirmLoading={createMutation.isPending}
        onCancel={() => setCreateOpen(false)}
        onOk={submitCreate}
      >
        <Form
          form={createForm}
          layout="vertical"
          className="task-create-form scheduled-task-form"
          initialValues={newScheduledTaskValues()}
        >
          <ScheduledTaskEditorFields
            scheduleTypes={scheduleTypes}
            actionTypes={actionTypes}
            bindings={triggers}
          />
        </Form>
      </Modal>

      <Drawer
        open={Boolean(selected)}
        width={720}
        destroyOnHidden
        className="task-inspector management-drawer"
        title={
          <div className="drawer-title-block">
            <span>{selected?.name}</span>
            <small>{selected?.id} · v{selected?.version}</small>
          </div>
        }
        extra={
          selected ? (
            <Button
              icon={<PlayCircleOutlined />}
              loading={
                runMutation.isPending
                && runMutation.variables === selected.id
              }
              onClick={() => runMutation.mutate(selected.id)}
            >
              立即运行
            </Button>
          ) : undefined
        }
        onClose={() => setSelected(null)}
        footer={
          drawerTab === "settings" ? (
            <div className="drawer-footer">
              <Typography.Text type="secondary">
                当前定义版本 {selected?.version ?? "—"}
              </Typography.Text>
              <Space>
                <Button onClick={() => setSelected(null)}>关闭</Button>
                <Button
                  type="primary"
                  loading={updateMutation.isPending}
                  onClick={submitUpdate}
                >
                  保存并更新计时器
                </Button>
              </Space>
            </div>
          ) : null
        }
      >
        {selected?.scheduler_error && (
          <Alert
            className="management-alert"
            type="error"
            showIcon
            message="调度器未能安装该任务"
            description={selected.scheduler_error}
          />
        )}
        <Descriptions
          className="management-drawer-summary"
          size="small"
          column={2}
          items={[
            {
              key: "next",
              label: "下次执行",
              children: formatTime(selected?.next_run_at ?? null),
            },
            {
              key: "last",
              label: "最近执行",
              children: formatTime(selected?.last_run_at ?? null),
            },
          ]}
        />
        <Tabs
          activeKey={drawerTab}
          onChange={setDrawerTab}
          items={[
            {
              key: "settings",
              label: <Space><EditOutlined />配置</Space>,
              children: (
                <Form
                  form={editForm}
                  layout="vertical"
                  className="inspector-form scheduled-task-form"
                >
                  <ScheduledTaskEditorFields
                    editing
                    scheduleTypes={scheduleTypes}
                    actionTypes={actionTypes}
                    bindings={triggers}
                  />
                </Form>
              ),
            },
            {
              key: "history",
              label: <Space><HistoryOutlined />运行历史</Space>,
              children: historyPanel,
            },
          ]}
        />
      </Drawer>
    </div>
  );
}
