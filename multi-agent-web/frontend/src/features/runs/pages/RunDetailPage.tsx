import {
  ArrowLeftOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CopyOutlined,
  PauseCircleOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  App,
  Button,
  Collapse,
  Descriptions,
  Drawer,
  Empty,
  Progress,
  Result,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { coreApi } from "../../../shared/api/client";
import { specToDraft } from "../../../shared/lib/workflow";
import type { TaskInstanceRecord } from "../../../shared/types";
import { WorkflowCanvas } from "../../workflows/components/WorkflowCanvas";

const terminalStatuses = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

const statusMeta: Record<string, { color: string; label: string }> = {
  queued: { color: "geekblue", label: "队列中" },
  pending: { color: "default", label: "等待" },
  ready: { color: "cyan", label: "就绪" },
  running: { color: "processing", label: "执行中" },
  awaiting_approval: { color: "warning", label: "等待准入" },
  succeeded: { color: "success", label: "成功" },
  failed: { color: "error", label: "失败" },
  cancelled: { color: "default", label: "已取消" },
  interrupted: { color: "warning", label: "已中断" },
  blocked: { color: "error", label: "已阻塞" },
};

function prettyOutput(value: unknown): string {
  if (value === null || value === undefined || value === "") return "暂无输出";
  if (typeof value !== "string") return JSON.stringify(value, null, 2);
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function TaskInstanceDrawer({
  task,
  onClose,
}: {
  task: TaskInstanceRecord | null;
  onClose: () => void;
}) {
  const { message } = App.useApp();
  if (!task) return null;
  const meta = statusMeta[task.status] ?? { color: "default", label: task.status };
  const output = prettyOutput(task.final_output);
  return (
    <Drawer
      title={<div className="drawer-title-block"><span>任务执行详情</span><small>{task.task_id}</small></div>}
      open
      width={500}
      mask={false}
      onClose={onClose}
      extra={<Tag color={meta.color}>{meta.label}</Tag>}
    >
      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label="Provider">{task.spec?.provider ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="角色">{task.spec?.role ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="尝试次数">{task.attempt_count ?? 0}</Descriptions.Item>
        <Descriptions.Item label="Session ID">
          <Typography.Text copyable={Boolean(task.provider_session_id)}>
            {task.provider_session_id ?? "—"}
          </Typography.Text>
        </Descriptions.Item>
        <Descriptions.Item label="错误码">{task.error_code ?? "—"}</Descriptions.Item>
      </Descriptions>
      {task.error_message && (
        <div className="run-error-box">
          <Typography.Text type="danger">{task.error_message}</Typography.Text>
        </div>
      )}
      <div className="output-heading">
        <Typography.Title level={5}>结构化输出</Typography.Title>
        <Button
          type="text"
          icon={<CopyOutlined />}
          onClick={() => navigator.clipboard.writeText(output).then(() => message.success("输出已复制"))}
        >
          复制
        </Button>
      </div>
      <pre className="output-viewer">{output}</pre>
    </Drawer>
  );
}

export function InstanceDetailPage() {
  const { instanceId = "" } = useParams();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [eventsOpen, setEventsOpen] = useState(true);

  const instanceQuery = useQuery({
    queryKey: ["instance", instanceId],
    queryFn: () => coreApi.getInstance(instanceId),
    enabled: Boolean(instanceId),
    refetchInterval: (query) =>
      terminalStatuses.has(query.state.data?.status ?? "") ? false : 1000,
  });
  const tasksQuery = useQuery({
    queryKey: ["instance-tasks", instanceId],
    queryFn: () => coreApi.getTaskInstances(instanceId),
    enabled: Boolean(instanceId),
    refetchInterval: () =>
      terminalStatuses.has(instanceQuery.data?.status ?? "") ? false : 1000,
  });
  const cancelMutation = useMutation({
    mutationFn: () => coreApi.cancelInstance(instanceId),
    onSuccess: () => {
      message.success("已发送取消请求");
      instanceQuery.refetch();
      tasksQuery.refetch();
    },
    onError: (error: Error) => message.error(error.message),
  });

  const taskRuns = tasksQuery.data ?? [];
  const taskDrafts = useMemo(() => {
    const specs = instanceQuery.data?.definition.tasks
      ?? taskRuns.flatMap((task) => (task.spec ? [task.spec] : []));
    return specs.map((task) => specToDraft(task));
  }, [instanceQuery.data?.definition.tasks, taskRuns]);
  const statuses = useMemo(
    () => Object.fromEntries(taskRuns.map((task) => [task.task_id, task.status])),
    [taskRuns],
  );
  const selectedTask = taskRuns.find((task) => task.task_id === selectedTaskId) ?? null;
  const completed = taskRuns.filter((task) => terminalStatuses.has(task.status) || task.status === "blocked").length;
  const progress = taskRuns.length ? Math.round((completed / taskRuns.length) * 100) : 0;
  const status = instanceQuery.data?.status ?? "queued";
  const meta = statusMeta[status] ?? { color: "default", label: status };

  if (instanceQuery.isLoading) {
    return <div className="page centered-state"><Spin size="large" tip="正在读取实例详情" /></div>;
  }
  if (instanceQuery.isError) {
    return (
      <Result
        status="error"
        title="无法读取工作流实例"
        subTitle={(instanceQuery.error as Error).message}
        extra={<Button onClick={() => navigate("/instances")}>返回实例中心</Button>}
      />
    );
  }

  return (
    <div className="page run-detail-page">
      <div className="page-heading run-heading">
        <div>
          <Button type="link" className="back-link" icon={<ArrowLeftOutlined />} onClick={() => navigate("/instances")}>
            返回工作流实例
          </Button>
          <div className="page-kicker">INSTANCE DETAIL <Tag color={meta.color}>{meta.label}</Tag></div>
          <Typography.Title level={2}>{instanceQuery.data?.name ?? "工作流实例"}</Typography.Title>
          <Space size={8} wrap>
            <Typography.Text type="secondary" copyable>{instanceId}</Typography.Text>
            {instanceQuery.data?.source === "template" && (
              <Tag bordered={false} color="blue">
                模板 {instanceQuery.data.template_id} · v{instanceQuery.data.template_version}
              </Tag>
            )}
          </Space>
        </div>
        <Space>
          {!terminalStatuses.has(status) && (
            <Button
              danger
              icon={<StopOutlined />}
              loading={cancelMutation.isPending}
              onClick={() => cancelMutation.mutate()}
            >
              取消实例
            </Button>
          )}
        </Space>
      </div>

      <div className="run-overview-bar">
        <div className="run-progress-copy">
          <span>整体进度</span>
          <strong>{completed} / {taskRuns.length || taskDrafts.length} 节点完成</strong>
        </div>
        <Progress percent={progress} status={status === "failed" ? "exception" : status === "succeeded" ? "success" : "active"} />
      </div>

      <section className="workflow-canvas-panel run-canvas" aria-label="工作流实例执行图">
        <div className="canvas-legend">
          <span><i className="status-dot running" />执行中</span>
          <span><i className="status-dot succeeded" />成功</span>
          <span><i className="status-dot failed" />失败或阻塞</span>
          <small>点击节点查看输出和错误详情</small>
        </div>
        {taskDrafts.length ? (
          <WorkflowCanvas
            tasks={taskDrafts}
            statuses={statuses}
            selectedTaskId={selectedTaskId}
            readOnly
            onSelectTask={setSelectedTaskId}
          />
        ) : (
          <div className="centered-state"><Empty description="实例尚未生成任务节点" /></div>
        )}
      </section>

      <div className="run-event-panel">
        <Collapse
          ghost
          activeKey={eventsOpen ? ["status"] : []}
          onChange={(keys) => setEventsOpen(keys.includes("status"))}
          items={[
            {
              key: "status",
              label: <strong>节点状态流</strong>,
              extra: <Typography.Text type="secondary">每秒自动刷新</Typography.Text>,
              children: (
                <div className="status-timeline">
                  {taskRuns.map((task) => {
                    const taskMeta = statusMeta[task.status] ?? { color: "default", label: task.status };
                    const icon = task.status === "succeeded"
                      ? <CheckCircleOutlined />
                      : task.status === "failed" || task.status === "blocked"
                        ? <CloseCircleOutlined />
                        : <PauseCircleOutlined />;
                    return (
                      <button key={task.task_id} type="button" onClick={() => setSelectedTaskId(task.task_id)}>
                        <span className={`timeline-icon ${task.status}`}>{icon}</span>
                        <span><strong>{task.task_id}</strong><small>{task.spec?.provider ?? "provider"}</small></span>
                        <Tag color={taskMeta.color} bordered={false}>{taskMeta.label}</Tag>
                      </button>
                    );
                  })}
                </div>
              ),
            },
          ]}
        />
      </div>
      <TaskInstanceDrawer task={selectedTask} onClose={() => setSelectedTaskId(null)} />
    </div>
  );
}
