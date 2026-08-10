import { Form, InputNumber, Modal, Select } from "antd";
import { useEffect } from "react";
import type { FailurePolicy } from "../../../shared/types";

interface WorkflowSettingsModalProps {
  open: boolean;
  maxConcurrency: number;
  failurePolicy: FailurePolicy;
  onCancel: () => void;
  onSave: (values: { maxConcurrency: number; failurePolicy: FailurePolicy }) => void;
}

export function WorkflowSettingsModal({
  open,
  maxConcurrency,
  failurePolicy,
  onCancel,
  onSave,
}: WorkflowSettingsModalProps) {
  const [form] = Form.useForm();
  useEffect(() => {
    if (open) form.setFieldsValue({ maxConcurrency, failurePolicy });
  }, [failurePolicy, form, maxConcurrency, open]);

  return (
    <Modal
      title="工作流运行设置"
      open={open}
      okText="保存设置"
      cancelText="取消"
      onCancel={onCancel}
      onOk={() => form.validateFields().then(onSave)}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" initialValues={{ maxConcurrency, failurePolicy }}>
        <Form.Item
          name="maxConcurrency"
          label="最大并发任务数"
          rules={[{ required: true, message: "请输入最大并发数" }]}
        >
          <InputNumber min={1} max={64} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="failurePolicy" label="失败策略" rules={[{ required: true }]}>
          <Select
            options={[
              { value: "continue_independent", label: "独立分支继续执行" },
              { value: "fail_fast", label: "任一失败后快速停止" },
            ]}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
