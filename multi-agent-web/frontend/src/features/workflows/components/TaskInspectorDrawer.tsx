import {
  CopyOutlined,
  DeleteOutlined,
  FormatPainterOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Drawer,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tabs,
  Typography,
  message,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import type { ProviderDescription, TaskDraft, WorkspaceMap } from "../../../shared/types";
import {
  identifierPattern,
  modelsForProvider,
  strictObjectSchemaTemplate,
  taskDraftSchema,
} from "../../../shared/lib/workflow";

interface TaskInspectorDrawerProps {
  task: TaskDraft | null;
  tasks: TaskDraft[];
  providers: ProviderDescription[];
  workspaces: WorkspaceMap;
  onClose: () => void;
  onSave: (originalId: string, task: TaskDraft) => void;
  onDelete: (taskId: string) => void;
  onDuplicate: (taskId: string) => void;
}

export function TaskInspectorDrawer({
  task,
  tasks,
  providers,
  workspaces,
  onClose,
  onSave,
  onDelete,
  onDuplicate,
}: TaskInspectorDrawerProps) {
  const [draft, setDraft] = useState<TaskDraft | null>(task ? { ...task } : null);
  const [messageApi, contextHolder] = message.useMessage();

  useEffect(() => {
    setDraft(task ? { ...task, depends_on: [...task.depends_on] } : null);
  }, [task]);

  const models = useMemo(
    () => (draft ? modelsForProvider(providers, draft.provider) : []),
    [draft?.provider, providers],
  );
  const modelTypes = [...new Set(models.map((model) => model.model_type))].sort();
  const selectableModels = models.filter((model) => model.model_type === draft?.model_type);
  const efforts = models.find((model) => model.id === draft?.model)?.efforts ?? [];
  const provider = providers.find((candidate) => candidate.name === draft?.provider);

  if (!draft || !task) {
    return <>{contextHolder}</>;
  }

  const update = <K extends keyof TaskDraft>(key: K, value: TaskDraft[K]) => {
    setDraft((current) => (current ? { ...current, [key]: value } : current));
  };

  const save = () => {
    const result = taskDraftSchema.safeParse(draft);
    if (!result.success) {
      messageApi.error(result.error.issues[0]?.message ?? "任务配置无效");
      return;
    }
    if (!identifierPattern.test(draft.id)) {
      messageApi.error("任务 ID 格式无效");
      return;
    }
    if (draft.id !== task.id && tasks.some((candidate) => candidate.id === draft.id)) {
      messageApi.error("任务 ID 已存在");
      return;
    }
    if (models.length) {
      const selectedModel = models.find(
        (model) => model.id === draft.model && model.model_type === draft.model_type,
      );
      if (!selectedModel) {
        messageApi.error("必须从模型目录中选择任务模型");
        return;
      }
      if (!selectedModel.efforts.includes(draft.effort)) {
        messageApi.error("必须显式选择有效的推理等级");
        return;
      }
    }
    if (draft.session_mode === "resume" && !draft.provider_session_id.trim()) {
      messageApi.error("恢复会话时必须填写 Provider Session ID");
      return;
    }
    onSave(task.id, draft);
    messageApi.success("任务参数已保存");
  };

  const formatSchema = () => {
    if (!draft.output_schema_text.trim()) return;
    try {
      update("output_schema_text", JSON.stringify(JSON.parse(draft.output_schema_text), null, 2));
    } catch {
      messageApi.error("输出 Schema 不是有效 JSON");
    }
  };

  const basicTab = (
    <Form layout="vertical" className="inspector-form">
      <Form.Item label="任务 ID" required>
        <Input value={draft.id} onChange={(event) => update("id", event.target.value)} />
      </Form.Item>
      <Form.Item label="任务角色" required>
        <Input value={draft.role} onChange={(event) => update("role", event.target.value)} />
      </Form.Item>
      <Form.Item label="授权工作区" required>
        <Select
          value={draft.workspace_id}
          options={Object.keys(workspaces).map((value) => ({ value, label: value }))}
          onChange={(value) => update("workspace_id", value)}
        />
      </Form.Item>
      <Form.Item label="依赖任务" extra="未选择依赖时，该任务可在工作流开始后立即就绪。">
        <Select
          mode="multiple"
          value={draft.depends_on}
          options={tasks
            .filter((candidate) => candidate.id !== task.id)
            .map((candidate) => ({ value: candidate.id, label: candidate.id }))}
          onChange={(value) => update("depends_on", value)}
          placeholder="选择一个或多个上游任务"
        />
      </Form.Item>
      <div className="two-column-form compact">
        <Form.Item label="访问权限">
          <Select
            value={draft.access}
            options={[
              { value: "read_only", label: "只读" },
              {
                value: "workspace_write",
                label: "工作区写入",
                disabled: !provider?.capabilities.workspace_write_mode,
              },
            ]}
            onChange={(value) => update("access", value)}
          />
        </Form.Item>
        <Form.Item label="超时（秒）">
          <InputNumber
            min={1}
            max={86_400}
            value={draft.timeout_seconds}
            onChange={(value) => update("timeout_seconds", value ?? 300)}
            style={{ width: "100%" }}
          />
        </Form.Item>
      </div>
    </Form>
  );

  const modelTab = (
    <Form layout="vertical" className="inspector-form">
      <Alert
        type="info"
        showIcon
        message="模型与推理等级必须由任务显式声明，不会使用 Codex 默认配置补全。"
      />
      <Form.Item label="Provider" required>
        <Select
          value={draft.provider}
          options={providers
            .filter((candidate) => candidate.available !== false)
            .map((candidate) => ({ value: candidate.name, label: candidate.name }))}
          onChange={(value) => {
            setDraft((current) =>
              current
                ? {
                    ...current,
                    provider: value,
                    access: "read_only",
                    model_type: "",
                    model: "",
                    effort: "",
                  }
                : current,
            );
          }}
        />
      </Form.Item>
      {models.length ? (
        <>
          <Form.Item label="模型类型" required>
            <Select
              value={draft.model_type || undefined}
              options={modelTypes.map((value) => ({ value, label: value }))}
              onChange={(value) => {
                setDraft((current) =>
                  current ? { ...current, model_type: value, model: "", effort: "" } : current,
                );
              }}
              placeholder="选择模型类型"
            />
          </Form.Item>
          <Form.Item label="任务模型" required>
            <Select
              value={draft.model || undefined}
              disabled={!draft.model_type}
              options={selectableModels.map((model) => ({
                value: model.id,
                label: model.label === model.id ? model.id : `${model.label} · ${model.id}`,
              }))}
              onChange={(value) => {
                setDraft((current) =>
                  current ? { ...current, model: value, effort: "" } : current,
                );
              }}
              placeholder="选择模型"
            />
          </Form.Item>
          <Form.Item label="推理等级" required>
            <Select
              value={draft.effort || undefined}
              disabled={!draft.model}
              options={efforts.map((value) => ({ value, label: value }))}
              onChange={(value) => update("effort", value)}
              placeholder="显式选择"
            />
          </Form.Item>
        </>
      ) : (
        <Alert
          type="warning"
          showIcon
          message="该 Provider 当前没有发布可选模型目录。"
          description="如果这是 Codex Provider，请刷新 OpenCodex 模型目录后重新加载页面。"
        />
      )}
    </Form>
  );

  const contractTab = (
    <Form layout="vertical" className="inspector-form contract-form">
      <Form.Item label="任务提示词" required>
        <Input.TextArea
          rows={9}
          value={draft.prompt_template}
          onChange={(event) => update("prompt_template", event.target.value)}
          placeholder="可以使用 {{tasks.task_id.output}} 引用上游任务输出。"
        />
      </Form.Item>
      <Form.Item
        label="输出 JSON Schema"
        extra={draft.provider === "codex" ? "Codex 对象 Schema 必须列出全部 required 字段并显式设置 additionalProperties: false。" : undefined}
      >
        <Input.TextArea
          className="schema-editor"
          rows={13}
          value={draft.output_schema_text}
          onChange={(event) => update("output_schema_text", event.target.value)}
          placeholder="可选；填写后要求 Provider 返回结构化结果。"
        />
      </Form.Item>
      <Space wrap>
        <Button icon={<FormatPainterOutlined />} onClick={formatSchema}>格式化 Schema</Button>
        <Button onClick={() => update("output_schema_text", strictObjectSchemaTemplate())}>
          使用严格对象模板
        </Button>
      </Space>
    </Form>
  );

  const advancedTab = (
    <Form layout="vertical" className="inspector-form">
      <div className="two-column-form compact">
        <Form.Item label="最大尝试次数">
          <InputNumber
            min={1}
            max={10}
            value={draft.max_attempts}
            onChange={(value) => update("max_attempts", value ?? 1)}
            style={{ width: "100%" }}
          />
        </Form.Item>
        <Form.Item label="任务幂等">
          <div className="switch-field">
            <Switch checked={draft.idempotent} onChange={(value) => update("idempotent", value)} />
            <Typography.Text type="secondary">写入任务重试时必须开启</Typography.Text>
          </div>
        </Form.Item>
      </div>
      <Form.Item label="会话方式">
        <Select
          value={draft.session_mode}
          options={[
            { value: "new", label: "新建独立会话" },
            { value: "resume", label: "恢复已有会话" },
          ]}
          onChange={(value) => {
            setDraft((current) =>
              current
                ? {
                    ...current,
                    session_mode: value,
                    provider_session_id: value === "new" ? "" : current.provider_session_id,
                  }
                : current,
            );
          }}
        />
      </Form.Item>
      {draft.session_mode === "resume" && (
        <Form.Item label="Provider Session ID" required>
          <Input
            value={draft.provider_session_id}
            onChange={(event) => update("provider_session_id", event.target.value)}
          />
        </Form.Item>
      )}
    </Form>
  );

  return (
    <>
      {contextHolder}
      <Drawer
        className="task-inspector"
        title={
          <div className="drawer-title-block">
            <span>任务参数</span>
            <small>{task.id}</small>
          </div>
        }
        width={480}
        open
        mask={false}
        onClose={onClose}
        extra={
          <Button type="primary" icon={<SaveOutlined />} onClick={save}>
            保存
          </Button>
        }
        footer={
          <div className="drawer-footer">
            <Button icon={<CopyOutlined />} onClick={() => onDuplicate(task.id)}>
              复制任务
            </Button>
            <Popconfirm
              title="删除任务节点？"
              description="依赖该任务的连线也会被移除。"
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => onDelete(task.id)}
            >
              <Button danger icon={<DeleteOutlined />}>删除</Button>
            </Popconfirm>
          </div>
        }
      >
        <Tabs
          defaultActiveKey="basic"
          items={[
            { key: "basic", label: "基础", children: basicTab },
            { key: "model", label: "模型", children: modelTab },
            { key: "contract", label: "契约", children: contractTab },
            { key: "advanced", label: "高级", children: advancedTab },
          ]}
        />
      </Drawer>
    </>
  );
}
