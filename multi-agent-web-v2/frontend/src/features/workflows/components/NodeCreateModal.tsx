import { Form, Input, Modal, Select } from "antd";
import { useEffect } from "react";
import type { CatalogModel, WorkflowNode } from "../../../shared/types";
import { createAgentNode } from "../model/workflow";

interface FormValue {
  id: string;
  model: string;
  effort: string;
  workspaceId: string;
  predecessors: string[];
}

interface Props {
  open: boolean;
  models: CatalogModel[];
  workspaces: string[];
  existingNodes: WorkflowNode[];
  onCancel: () => void;
  onCreate: (node: WorkflowNode, predecessors: string[]) => void;
}

export function NodeCreateModal({
  open,
  models,
  workspaces,
  existingNodes,
  onCancel,
  onCreate,
}: Props) {
  const [form] = Form.useForm<FormValue>();
  const selectedModel = Form.useWatch("model", form);
  const model = models.find((item) => item.id === selectedModel);

  useEffect(() => {
    if (!open) return;
    const first = models[0];
    form.setFieldsValue({
      id: `agent-${existingNodes.length + 1}`,
      model: first?.id,
      effort: first?.recommendedEffort ?? first?.efforts[0],
      workspaceId: workspaces[0],
      predecessors: existingNodes.length ? [existingNodes.at(-1)!.id] : [],
    });
  }, [existingNodes, form, models, open, workspaces]);

  useEffect(() => {
    if (!model) return;
    const effort = form.getFieldValue("effort");
    if (!model.efforts.includes(effort)) {
      form.setFieldValue("effort", model.recommendedEffort ?? model.efforts[0]);
    }
  }, [form, model]);

  return (
    <Modal
      open={open}
      title="添加 Agent 节点"
      okText="添加到工作流"
      cancelText="取消"
      onCancel={onCancel}
      onOk={() => form.submit()}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={(value) => {
          const catalogModel = models.find((item) => item.id === value.model);
          const node = createAgentNode(value.id, catalogModel, value.workspaceId);
          if (node.agent) {
            node.agent.model = value.model;
            node.agent.effort = value.effort;
          }
          onCreate(node, value.predecessors ?? []);
          form.resetFields();
        }}
      >
        <Form.Item
          name="id"
          label="节点 ID"
          rules={[
            { required: true, message: "请输入节点 ID" },
            { pattern: /^[a-z][a-z0-9_-]{0,63}$/, message: "使用小写字母、数字、_ 或 -" },
            {
              validator: (_, value) =>
                existingNodes.some((item) => item.id === value)
                  ? Promise.reject(new Error("节点 ID 已存在"))
                  : Promise.resolve(),
            },
          ]}
        >
          <Input placeholder="例如：review-code" />
        </Form.Item>
        <Form.Item name="predecessors" label="前置节点">
          <Select
            mode="multiple"
            allowClear
            options={existingNodes.map((node) => ({ value: node.id, label: node.id }))}
            placeholder="无前置节点时作为起点"
          />
        </Form.Item>
        <Form.Item name="model" label="Codex 模型" rules={[{ required: true }]}>
          <Select
            showSearch
            optionFilterProp="label"
            options={models.map((item) => ({
              value: item.id,
              label: `${item.label} · ${item.modelType}`,
            }))}
            placeholder="模型目录尚未就绪"
          />
        </Form.Item>
        <Form.Item name="effort" label="推理等级" rules={[{ required: true }]}>
          <Select
            options={(model?.efforts ?? []).map((effort) => ({
              value: effort,
              label: effort,
            }))}
          />
        </Form.Item>
        <Form.Item name="workspaceId" label="工作区" rules={[{ required: true }]}>
          <Select
            options={workspaces.map((workspace) => ({ value: workspace, label: workspace }))}
            placeholder="服务端尚未配置工作区"
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
