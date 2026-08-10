import {
  ApartmentOutlined,
  CheckCircleOutlined,
  ExperimentOutlined,
  PlusOutlined,
  RocketOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App,
  Badge,
  Button,
  Input,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { coreApi } from "../../../shared/api/client";
import { buildWorkflow } from "../../../shared/lib/workflow";
import { TaskCreateModal } from "../components/TaskCreateModal";
import { TaskInspectorDrawer } from "../components/TaskInspectorDrawer";
import { WorkflowCanvas } from "../components/WorkflowCanvas";
import { WorkflowSettingsModal } from "../components/WorkflowSettingsModal";
import { useWorkflowStore } from "../model/store";

function nextTaskId(ids: string[]): string {
  let index = 1;
  while (ids.includes(`task_${index}`)) index += 1;
  return `task_${index}`;
}

export function WorkflowEditorPage() {
  const navigate = useNavigate();
  const params = useParams();
  const queryClient = useQueryClient();
  const { message, modal } = App.useApp();
  const providersQuery = useQuery({ queryKey: ["providers"], queryFn: coreApi.providers });
  const workspacesQuery = useQuery({ queryKey: ["workspaces"], queryFn: coreApi.workspaces });
  const providers = providersQuery.data ?? [];
  const workspaces = workspacesQuery.data ?? {};

  const workflowName = useWorkflowStore((state) => state.workflowName);
  const maxConcurrency = useWorkflowStore((state) => state.maxConcurrency);
  const failurePolicy = useWorkflowStore((state) => state.failurePolicy);
  const tasks = useWorkflowStore((state) => state.tasks);
  const selectedTaskId = useWorkflowStore((state) => state.selectedTaskId);
  const createModalOpen = useWorkflowStore((state) => state.createModalOpen);
  const settingsModalOpen = useWorkflowStore((state) => state.settingsModalOpen);
  const setWorkflowName = useWorkflowStore((state) => state.setWorkflowName);
  const setWorkflowSettings = useWorkflowStore((state) => state.setWorkflowSettings);
  const setCreateModalOpen = useWorkflowStore((state) => state.setCreateModalOpen);
  const setSettingsModalOpen = useWorkflowStore((state) => state.setSettingsModalOpen);
  const selectTask = useWorkflowStore((state) => state.selectTask);
  const addTask = useWorkflowStore((state) => state.addTask);
  const updateTask = useWorkflowStore((state) => state.updateTask);
  const removeTask = useWorkflowStore((state) => state.removeTask);
  const duplicateTask = useWorkflowStore((state) => state.duplicateTask);
  const loadAdditionSample = useWorkflowStore((state) => state.loadAdditionSample);

  const selectedTask = useMemo(
    () => tasks.find((task) => task.id === selectedTaskId) ?? null,
    [selectedTaskId, tasks],
  );
  const catalogReady = providers.length > 0 && Object.keys(workspaces).length > 0;
  const configuredTasks = tasks.filter((task) => task.model && task.effort).length;

  const workflow = () =>
    buildWorkflow(workflowName, tasks, providers, maxConcurrency, failurePolicy);

  const validateMutation = useMutation({
    mutationFn: async () => coreApi.validateWorkflow(workflow()),
    onSuccess: (result) => message.success(`校验通过，共 ${result.task_count} 个任务`),
    onError: (error: Error) => message.error(error.message),
  });
  const runMutation = useMutation({
    mutationFn: async () => {
      const definition = workflow();
      await coreApi.validateWorkflow(definition);
      return coreApi.createRun(definition);
    },
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ["run", run.id] });
      message.success("工作流已提交");
      navigate(`/runs/${run.id}`);
    },
    onError: (error: Error) => message.error(error.message),
  });

  const confirmDelete = (taskId: string) => {
    modal.confirm({
      title: `删除任务 ${taskId}？`,
      content: "依赖该任务的连线也会一并移除。",
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => removeTask(taskId),
    });
  };

  const loading = providersQuery.isLoading || workspacesQuery.isLoading;
  return (
    <div className="page editor-page">
      <div className="page-heading editor-heading">
        <div>
          <div className="page-kicker">
            <ApartmentOutlined /> 工作流编排
            {params.workflowId && <Tag bordered={false}>{params.workflowId}</Tag>}
          </div>
          <Input
            className="workflow-title-input"
            value={workflowName}
            onChange={(event) => setWorkflowName(event.target.value)}
            aria-label="工作流名称"
            maxLength={200}
          />
          <Typography.Paragraph type="secondary">
            在画布中关注任务依赖与执行状态；详细参数由弹窗和右侧面板承载。
          </Typography.Paragraph>
        </div>
        <Space wrap className="editor-actions">
          <Tooltip title="加载两阶段加法示例；模型与推理等级仍需逐任务显式选择">
            <Button
              icon={<ExperimentOutlined />}
              disabled={!catalogReady}
              onClick={() => {
                loadAdditionSample(providers, Object.keys(workspaces));
                message.info("示例已加载，请为每个任务选择模型和推理等级");
              }}
            >
              加法示例
            </Button>
          </Tooltip>
          <Button icon={<SettingOutlined />} onClick={() => setSettingsModalOpen(true)}>
            运行设置
          </Button>
          <Button
            icon={<CheckCircleOutlined />}
            disabled={!tasks.length}
            loading={validateMutation.isPending}
            onClick={() => validateMutation.mutate()}
          >
            校验
          </Button>
          <Button
            type="primary"
            icon={<RocketOutlined />}
            disabled={!tasks.length}
            loading={runMutation.isPending}
            onClick={() => runMutation.mutate()}
          >
            提交运行
          </Button>
        </Space>
      </div>

      <div className="composer-toolbar">
        <Space size={18} wrap>
          <span><Badge status={catalogReady ? "success" : "processing"} />{catalogReady ? "执行目录已就绪" : "正在读取执行目录"}</span>
          <span><strong>{tasks.length}</strong> 个任务</span>
          <span><strong>{configuredTasks}</strong> 个已配置模型</span>
          <span>并发 <strong>{maxConcurrency}</strong></span>
          <Tag bordered={false} color={failurePolicy === "fail_fast" ? "orange" : "blue"}>
            {failurePolicy === "fail_fast" ? "快速失败" : "独立分支继续"}
          </Tag>
        </Space>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          disabled={!catalogReady}
          onClick={() => setCreateModalOpen(true)}
        >
          添加任务
        </Button>
      </div>

      <section className="workflow-canvas-panel" aria-label="工作流执行图">
        <div className="canvas-legend">
          <span><i className="legend-node" />任务节点</span>
          <span><i className="legend-edge" />依赖方向</span>
          <small>点击节点，在右侧修改参数</small>
        </div>
        {loading ? (
          <div className="centered-state"><Spin size="large" tip="正在读取执行目录" /></div>
        ) : (
          <WorkflowCanvas
            tasks={tasks}
            selectedTaskId={selectedTaskId}
            onSelectTask={selectTask}
            onDeleteTask={confirmDelete}
          />
        )}
      </section>

      <TaskCreateModal
        open={createModalOpen}
        providers={providers}
        workspaces={workspaces}
        nextId={nextTaskId(tasks.map((task) => task.id))}
        existingIds={tasks.map((task) => task.id)}
        onCancel={() => setCreateModalOpen(false)}
        onCreate={addTask}
      />
      <WorkflowSettingsModal
        open={settingsModalOpen}
        maxConcurrency={maxConcurrency}
        failurePolicy={failurePolicy}
        onCancel={() => setSettingsModalOpen(false)}
        onSave={(values) => {
          setWorkflowSettings(values);
          setSettingsModalOpen(false);
        }}
      />
      <TaskInspectorDrawer
        task={selectedTask}
        tasks={tasks}
        providers={providers}
        workspaces={workspaces}
        onClose={() => selectTask(null)}
        onSave={updateTask}
        onDelete={removeTask}
        onDuplicate={duplicateTask}
      />
    </div>
  );
}
