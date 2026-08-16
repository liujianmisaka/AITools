import { DeleteOutlined } from "@ant-design/icons";
import { Button, Collapse, Drawer, Form, Input, InputNumber, Select, Space, Switch } from "antd";
import { useEffect, useMemo } from "react";
import type {
  CatalogModel,
  WorkflowNode,
  WorkflowTransition,
} from "../../../shared/types";

interface Props {
  node: WorkflowNode | null;
  nodes: WorkflowNode[];
  transitions: WorkflowTransition[];
  models: CatalogModel[];
  workspaces: string[];
  onClose: () => void;
  onChange: (node: WorkflowNode, predecessors: string[]) => void;
  onDelete: (nodeId: string) => void;
}

interface FormValue {
  instruction: string;
  model: string;
  effort: string;
  workspaceId: string;
  access: "read_only" | "workspace_write";
  sessionMode: "new" | "resume";
  timeout: string;
  maximumAttempts: number;
  predecessors: string[];
  outputSchema: string;
}

export function NodeInspectorDrawer({
  node,
  nodes,
  transitions,
  models,
  workspaces,
  onClose,
  onChange,
  onDelete,
}: Props) {
  const [form] = Form.useForm<FormValue>();
  const selectedModel = Form.useWatch("model", form);
  const model = models.find((item) => item.id === selectedModel);
  const predecessors = useMemo(
    () => transitions.filter((item) => item.to === node?.id).map((item) => item.from),
    [node?.id, transitions],
  );

  useEffect(() => {
    if (!node) return;
    form.setFieldsValue({
      instruction: node.agent?.instruction ?? "",
      model: node.agent?.model ?? "",
      effort: node.agent?.effort ?? "",
      workspaceId: node.agent?.workspaceId ?? "",
      access: node.agent?.access ?? "read_only",
      sessionMode: node.agent?.sessionMode ?? "new",
      timeout: node.agent?.timeout ?? "PT5M",
      maximumAttempts: node.agent?.retry.maximumAttempts ?? 1,
      predecessors,
      outputSchema: JSON.stringify(node.outputSchema, null, 2),
    });
  }, [form, node, predecessors]);

  const commit = async () => {
    if (!node) return;
    const value = await form.validateFields();
    let outputSchema: Record<string, unknown>;
    try {
      outputSchema = JSON.parse(value.outputSchema) as Record<string, unknown>;
    } catch {
      form.setFields([{ name: "outputSchema", errors: ["必须是有效 JSON"] }]);
      return;
    }
    onChange(
      {
        ...node,
        outputSchema,
        agent: node.agent
          ? {
              ...node.agent,
              instruction: value.instruction,
              model: value.model,
              effort: value.effort,
              workspaceId: value.workspaceId,
              access: value.access,
              sessionMode: value.sessionMode,
              timeout: value.timeout,
              retry: { maximumAttempts: value.maximumAttempts },
            }
          : node.agent,
      },
      value.predecessors ?? [],
    );
  };

  return (
    <Drawer
      open={Boolean(node)}
      title={
        <div className="drawer-title">
          <span>节点参数</span>
          <small>{node?.id}</small>
        </div>
      }
      width={430}
      onClose={onClose}
      extra={
        <Space>
          <Button
            danger
            type="text"
            icon={<DeleteOutlined />}
            onClick={() => node && onDelete(node.id)}
          />
          <Button type="primary" onClick={() => void commit()}>
            应用修改
          </Button>
        </Space>
      }
    >
      {node && (
        <Form form={form} layout="vertical">
          <Collapse
            bordered={false}
            defaultActiveKey={["basic", "model"]}
            items={[
              {
                key: "basic",
                label: "基础与执行流",
                children: (
                  <>
                    <Form.Item name="predecessors" label="前置节点">
                      <Select
                        mode="multiple"
                        options={nodes
                          .filter((item) => item.id !== node.id)
                          .map((item) => ({ value: item.id, label: item.id }))}
                      />
                    </Form.Item>
                    <Form.Item
                      name="instruction"
                      label="任务说明"
                      rules={[{ required: Boolean(node.agent), message: "请输入任务说明" }]}
                    >
                      <Input.TextArea autoSize={{ minRows: 5, maxRows: 12 }} />
                    </Form.Item>
                  </>
                ),
              },
              {
                key: "model",
                label: "模型与工作区",
                children: node.agent ? (
                  <>
                    <Form.Item name="model" label="Codex 模型" rules={[{ required: true }]}>
                      <Select
                        showSearch
                        optionFilterProp="label"
                        options={models.map((item) => ({
                          value: item.id,
                          label: `${item.label} · ${item.modelType}`,
                        }))}
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
                        options={workspaces.map((value) => ({ value, label: value }))}
                      />
                    </Form.Item>
                    <Form.Item name="access" label="工作区权限">
                      <Select
                        options={[
                          { value: "read_only", label: "只读" },
                          { value: "workspace_write", label: "隔离 worktree 写入" },
                        ]}
                      />
                    </Form.Item>
                    <Form.Item name="sessionMode" label="会话模式">
                      <Select
                        options={[
                          { value: "new", label: "新会话" },
                          { value: "resume", label: "恢复指定会话" },
                        ]}
                      />
                    </Form.Item>
                  </>
                ) : (
                  <span>该节点不是 Agent 节点，模型设置不适用。</span>
                ),
              },
              {
                key: "contract",
                label: "输出契约",
                children: (
                  <Form.Item name="outputSchema" label="严格 JSON Schema">
                    <Input.TextArea className="schema-editor" autoSize={{ minRows: 10 }} />
                  </Form.Item>
                ),
              },
              {
                key: "advanced",
                label: "高级",
                children: (
                  <>
                    <Form.Item name="timeout" label="超时（ISO 8601）">
                      <Input placeholder="PT5M" />
                    </Form.Item>
                    <Form.Item name="maximumAttempts" label="最大尝试次数">
                      <InputNumber min={1} max={10} />
                    </Form.Item>
                    <Form.Item label="使用 Provider 默认模型">
                      <Switch disabled checked={false} />
                      <span className="field-note">平台禁止默认模型，必须任务级显式选择。</span>
                    </Form.Item>
                  </>
                ),
              },
            ]}
          />
        </Form>
      )}
    </Drawer>
  );
}
