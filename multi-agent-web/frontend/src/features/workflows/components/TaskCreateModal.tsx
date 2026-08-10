import { zodResolver } from "@hookform/resolvers/zod";
import { Alert, Form, Input, Modal, Select } from "antd";
import { useEffect, useMemo } from "react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";
import type { AccessMode, ProviderDescription, TaskDraft, WorkspaceMap } from "../../../shared/types";
import { createTaskDraft, identifierPattern, modelsForProvider } from "../../../shared/lib/workflow";

const createSchema = z.object({
  id: z.string().min(1, "请输入任务 ID").regex(identifierPattern, "任务 ID 格式无效"),
  provider: z.string().min(1, "请选择 Provider"),
  role: z.string().min(1, "请输入任务角色"),
  workspace_id: z.string().min(1, "请选择工作区"),
  access: z.enum(["read_only", "workspace_write"]),
  prompt_template: z.string().min(1, "请输入任务提示词"),
  model_type: z.string(),
  model: z.string(),
  effort: z.string(),
});

type CreateFields = z.infer<typeof createSchema>;

interface TaskCreateModalProps {
  open: boolean;
  providers: ProviderDescription[];
  workspaces: WorkspaceMap;
  nextId: string;
  existingIds: string[];
  onCancel: () => void;
  onCreate: (task: TaskDraft) => void;
}

export function TaskCreateModal({
  open,
  providers,
  workspaces,
  nextId,
  existingIds,
  onCancel,
  onCreate,
}: TaskCreateModalProps) {
  const workspaceIds = Object.keys(workspaces);
  const defaults = useMemo(
    () => createTaskDraft(providers, workspaceIds, nextId),
    [nextId, providers, workspaceIds.join("|")],
  );
  const {
    control,
    handleSubmit,
    reset,
    setError,
    setValue,
    watch,
    formState: { errors },
  } = useForm<CreateFields>({
    resolver: zodResolver(createSchema),
    defaultValues: defaults,
  });

  useEffect(() => {
    if (open) reset(defaults);
  }, [defaults, open, reset]);

  const providerName = watch("provider");
  const modelType = watch("model_type");
  const modelId = watch("model");
  const models = modelsForProvider(providers, providerName);
  const modelTypes = [...new Set(models.map((model) => model.model_type))].sort();
  const selectableModels = models.filter((model) => model.model_type === modelType);
  const efforts = models.find((model) => model.id === modelId)?.efforts ?? [];
  const selectedProvider = providers.find((provider) => provider.name === providerName);

  const submit = handleSubmit((values) => {
    if (existingIds.includes(values.id)) {
      setError("id", { message: "任务 ID 已存在" });
      return;
    }
    if (models.length) {
      if (!values.model_type) {
        setError("model_type", { message: "请选择模型类型" });
        return;
      }
      if (!models.some((model) => model.id === values.model && model.model_type === values.model_type)) {
        setError("model", { message: "请从目录选择模型" });
        return;
      }
      if (!efforts.includes(values.effort)) {
        setError("effort", { message: "请选择推理等级" });
        return;
      }
    }
    onCreate({ ...defaults, ...values, access: values.access as AccessMode });
  });

  return (
    <Modal
      title={
        <div className="modal-title-block">
          <span>创建任务节点</span>
          <small>先完成执行所需的最小契约，详细参数可在右侧面板继续调整。</small>
        </div>
      }
      open={open}
      width={760}
      okText="创建并编辑"
      cancelText="取消"
      onCancel={onCancel}
      onOk={submit}
      destroyOnHidden
    >
      {providers.length === 0 && (
        <Alert type="warning" showIcon message="执行目录尚未就绪，暂时无法创建可运行任务。" />
      )}
      <Form layout="vertical" className="task-create-form">
        <div className="two-column-form">
          <Controller
            name="id"
            control={control}
            render={({ field }) => (
              <Form.Item label="任务 ID" required validateStatus={errors.id ? "error" : ""} help={errors.id?.message}>
                <Input {...field} placeholder="例如 extract_context" />
              </Form.Item>
            )}
          />
          <Controller
            name="role"
            control={control}
            render={({ field }) => (
              <Form.Item label="任务角色" required validateStatus={errors.role ? "error" : ""} help={errors.role?.message}>
                <Input {...field} placeholder="例如 researcher" />
              </Form.Item>
            )}
          />
          <Controller
            name="provider"
            control={control}
            render={({ field }) => (
              <Form.Item label="Provider" required validateStatus={errors.provider ? "error" : ""} help={errors.provider?.message}>
                <Select
                  {...field}
                  options={providers
                    .filter((provider) => provider.available !== false)
                    .map((provider) => ({ value: provider.name, label: provider.name }))}
                  onChange={(value) => {
                    field.onChange(value);
                    setValue("model_type", "");
                    setValue("model", "");
                    setValue("effort", "");
                  }}
                  placeholder="选择 Provider"
                />
              </Form.Item>
            )}
          />
          <Controller
            name="workspace_id"
            control={control}
            render={({ field }) => (
              <Form.Item label="授权工作区" required validateStatus={errors.workspace_id ? "error" : ""} help={errors.workspace_id?.message}>
                <Select {...field} options={workspaceIds.map((id) => ({ value: id, label: id }))} />
              </Form.Item>
            )}
          />
          <Controller
            name="access"
            control={control}
            render={({ field }) => (
              <Form.Item label="访问权限" required>
                <Select
                  {...field}
                  options={[
                    { value: "read_only", label: "只读" },
                    {
                      value: "workspace_write",
                      label: "工作区写入",
                      disabled: !selectedProvider?.capabilities.workspace_write_mode,
                    },
                  ]}
                />
              </Form.Item>
            )}
          />
        </div>
        {models.length > 0 && (
          <div className="three-column-form model-contract-row">
            <Controller
              name="model_type"
              control={control}
              render={({ field }) => (
                <Form.Item label="模型类型" required validateStatus={errors.model_type ? "error" : ""} help={errors.model_type?.message}>
                  <Select
                    {...field}
                    options={modelTypes.map((value) => ({ value, label: value }))}
                    placeholder="选择类型"
                    onChange={(value) => {
                      field.onChange(value);
                      setValue("model", "");
                      setValue("effort", "");
                    }}
                  />
                </Form.Item>
              )}
            />
            <Controller
              name="model"
              control={control}
              render={({ field }) => (
                <Form.Item label="任务模型" required validateStatus={errors.model ? "error" : ""} help={errors.model?.message}>
                  <Select
                    {...field}
                    disabled={!modelType}
                    options={selectableModels.map((model) => ({
                      value: model.id,
                      label: model.label === model.id ? model.id : `${model.label} · ${model.id}`,
                    }))}
                    placeholder="选择模型"
                    onChange={(value) => {
                      field.onChange(value);
                      setValue("effort", "");
                    }}
                  />
                </Form.Item>
              )}
            />
            <Controller
              name="effort"
              control={control}
              render={({ field }) => (
                <Form.Item label="推理等级" required validateStatus={errors.effort ? "error" : ""} help={errors.effort?.message}>
                  <Select
                    {...field}
                    disabled={!modelId}
                    options={efforts.map((value) => ({ value, label: value }))}
                    placeholder="显式选择"
                  />
                </Form.Item>
              )}
            />
          </div>
        )}
        <Controller
          name="prompt_template"
          control={control}
          render={({ field }) => (
            <Form.Item
              label="任务提示词"
              required
              validateStatus={errors.prompt_template ? "error" : ""}
              help={errors.prompt_template?.message}
            >
              <Input.TextArea {...field} rows={4} placeholder="描述该节点的职责、输入与期望输出。" />
            </Form.Item>
          )}
        />
      </Form>
    </Modal>
  );
}
