import {
  Alert,
  Form,
  Input,
  InputNumber,
  Select,
  Switch,
  Typography,
} from "antd";
import type {
  ScheduleTypeDescription,
  ScheduledActionTypeDescription,
  TriggerBindingRecord,
} from "../../../shared/types";
import type { ScheduledTaskEditorValues } from "../model/scheduledTaskForm";

interface ScheduledTaskEditorFieldsProps {
  scheduleTypes: ScheduleTypeDescription[];
  actionTypes: ScheduledActionTypeDescription[];
  bindings: TriggerBindingRecord[];
  editing?: boolean;
}

function bindingLabel(binding: TriggerBindingRecord): string {
  return `${binding.name} · ${binding.source_type} · ${binding.id}`;
}

export function ScheduledTaskEditorFields({
  scheduleTypes,
  actionTypes,
  bindings,
  editing = false,
}: ScheduledTaskEditorFieldsProps) {
  const eligibleBindings = bindings.filter(
    (binding) => binding.enabled && binding.source_type === "git_commit",
  );
  return (
    <>
      <div className="two-column-form">
        <Form.Item
          label="任务 ID"
          name="id"
          rules={[
            { required: true, message: "请输入任务 ID" },
            {
              pattern: /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/,
              message: "仅支持字母、数字、点、下划线和连字符",
            },
          ]}
        >
          <Input disabled={editing} />
        </Form.Item>
        <Form.Item
          label="任务名称"
          name="name"
          rules={[{ required: true, message: "请输入任务名称" }]}
        >
          <Input placeholder="例如：每五分钟检查主分支" />
        </Form.Item>
      </div>

      <Form.Item name="version" hidden>
        <InputNumber />
      </Form.Item>

      <div className="two-column-form">
        <Form.Item
          label="计划类型"
          name="schedule_type"
          rules={[{ required: true }]}
        >
          <Select
            disabled={editing}
            options={scheduleTypes.map((item) => ({
              value: item.schedule_type,
              label: item.schedule_type === "cron" ? "Cron 定时" : item.schedule_type,
            }))}
          />
        </Form.Item>
        <Form.Item
          label="动作类型"
          name="action_type"
          rules={[{ required: true }]}
        >
          <Select
            disabled={editing}
            options={actionTypes.map((item) => ({
              value: item.action_type,
              label:
                item.action_type === "poll_trigger_binding"
                  ? "轮询事件触发规则"
                  : item.action_type,
            }))}
          />
        </Form.Item>
      </div>

      <Form.Item
        label="Trigger Binding"
        name="binding_id"
        tooltip="只显示已启用且支持轮询的 Git 事件触发规则"
        rules={[{ required: true, message: "请选择要轮询的触发规则" }]}
      >
        <Select
          showSearch
          optionFilterProp="label"
          placeholder="选择已启用的 Git Trigger Binding"
          options={eligibleBindings.map((binding) => ({
            value: binding.id,
            label: bindingLabel(binding),
          }))}
        />
      </Form.Item>

      {!eligibleBindings.length && (
        <Alert
          className="form-context-alert"
          type="warning"
          showIcon
          message="没有可轮询的事件触发规则"
          description="请先在“事件触发”页面创建并启用 Git 分支提交规则。"
        />
      )}

      <Form.Item
        label="Cron 表达式"
        name="expression"
        tooltip="按分钟、小时、日、月、星期排列，共五段"
        rules={[
          { required: true, message: "请输入 Cron 表达式" },
          {
            validator: (_, value: string) =>
              value?.trim().split(/\s+/).length === 5
                ? Promise.resolve()
                : Promise.reject(new Error("Cron 表达式必须包含五个字段")),
          },
        ]}
      >
        <Input
          className="management-code-input"
          placeholder="*/5 * * * *"
        />
      </Form.Item>

      <div className="two-column-form">
        <Form.Item
          label="时区"
          name="timezone"
          rules={[{ required: true, message: "请选择时区" }]}
        >
          <Select
            showSearch
            options={[
              { value: "Asia/Shanghai", label: "Asia/Shanghai（中国标准时间）" },
              { value: "UTC", label: "UTC" },
              { value: "America/New_York", label: "America/New_York" },
              { value: "America/Los_Angeles", label: "America/Los_Angeles" },
              { value: "Europe/London", label: "Europe/London" },
            ]}
          />
        </Form.Item>
        <Form.Item
          label="错过执行宽限（秒）"
          name="misfire_grace_seconds"
          rules={[{ required: true, message: "请输入宽限时间" }]}
        >
          <InputNumber min={1} max={86_400} step={30} className="full-width-control" />
        </Form.Item>
      </div>

      <div className="scheduled-switch-grid">
        <Form.Item
          label="合并错过的执行"
          name="coalesce"
          valuePropName="checked"
          tooltip="服务恢复后，将多个错过的触发时间合并为一次"
        >
          <Switch checkedChildren="合并" unCheckedChildren="逐次" />
        </Form.Item>
        <Form.Item
          label={editing ? "启用任务" : "创建后启用"}
          name="enabled"
          valuePropName="checked"
        >
          <Switch checkedChildren="启用" unCheckedChildren="停用" />
        </Form.Item>
      </div>

      <Alert
        className="form-context-alert"
        type="info"
        showIcon
        message="调度定义会持久化保存"
        description={
          <Typography.Text type="secondary">
            服务重启时由核心重新恢复计时器；每次执行只负责轮询 Trigger Binding，不介入工作流任务细节。
          </Typography.Text>
        }
      />
    </>
  );
}
