import { Alert, Button, Input, Skeleton, Space, Tag, Typography } from "antd";
import type { JsonObject } from "../../../shared/types";
import { describeWorkflowInput } from "../model/workflow";

interface WorkflowInputEditorProps {
  schema?: JsonObject;
  loading?: boolean;
  error?: boolean;
  value: string;
  onChange: (value: string) => void;
  onReset: () => void;
}

export function WorkflowInputEditor({
  schema,
  loading = false,
  error = false,
  value,
  onChange,
  onReset,
}: WorkflowInputEditorProps) {
  if (loading) return <Skeleton active paragraph={{ rows: 5 }} />;
  if (error || !schema) {
    return <Alert type="error" showIcon message="无法读取模板输入说明" />;
  }

  const fields = describeWorkflowInput(schema);

  return (
    <div className="workflow-input-editor">
      {fields.length ? (
        <>
          <Alert
            type="success"
            showIcon
            message="已根据模板生成可运行示例"
            description="没有特殊要求时可以直接运行；需要区分本次请求时，再修改下方示例值。"
          />
          <Typography.Text strong>需要填写的参数</Typography.Text>
          <div className="workflow-input-fields">
            {fields.map((field) => (
              <div className="workflow-input-field" key={field.name}>
                <Space size={6} wrap>
                  <Typography.Text code>{field.name}</Typography.Text>
                  <Tag color={field.required ? "red" : "default"}>
                    {field.required ? "必填" : "可选"}
                  </Tag>
                  <Tag>{field.type}</Tag>
                </Space>
                <Typography.Text type="secondary">{field.description}</Typography.Text>
              </div>
            ))}
          </div>
        </>
      ) : (
        <Alert
          type="info"
          showIcon
          message="此工作流不需要输入参数"
          description="保持默认的空对象，直接点击运行即可。"
        />
      )}
      <div className="workflow-input-heading">
        <div>
          <Typography.Text strong>运行输入</Typography.Text>
          <Typography.Text type="secondary">JSON 对象，可在示例基础上修改</Typography.Text>
        </div>
        <Button type="link" size="small" onClick={onReset}>
          恢复示例
        </Button>
      </div>
      <Input.TextArea
        className="schema-editor"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoSize={{ minRows: 7, maxRows: 16 }}
      />
    </div>
  );
}
