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

function scheduleTypeLabel(scheduleType: string): string {
  if (scheduleType === "cron") return "Cron 定时";
  if (scheduleType === "interval") return "Interval 间隔";
  if (scheduleType === "one_time") return "One-time 一次性";
  return scheduleType;
}

function actionTypeLabel(actionType: string): string {
  if (actionType === "poll_trigger_binding") return "轮询事件触发规则";
  if (actionType === "publish_trigger_event") return "发布计划合成事件";
  return actionType;
}

export function ScheduledTaskEditorFields({
  scheduleTypes,
  actionTypes,
  bindings,
  editing = false,
}: ScheduledTaskEditorFieldsProps) {
  const scheduleType = Form.useWatch("schedule_type", { preserve: true });
  const actionType = Form.useWatch("action_type", { preserve: true });
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
              label: scheduleTypeLabel(item.schedule_type),
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
              label: actionTypeLabel(item.action_type),
            }))}
          />
        </Form.Item>
      </div>

      {actionType === "poll_trigger_binding" && (
        <>
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
        </>
      )}

      {scheduleType === "cron" && (
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
          <Input className="management-code-input" placeholder="*/5 * * * *" />
        </Form.Item>
      )}

      {scheduleType === "interval" && (
        <>
          <Form.Item label="Interval 间隔" required>
            <div className="three-column-form trigger-source-grid">
              <Form.Item
                label="秒"
                name="seconds"
                rules={[{ required: true, message: "请输入秒数" }]}
              >
                <InputNumber min={0} max={10_000} className="full-width-control" />
              </Form.Item>
              <Form.Item label="分钟" name="minutes">
                <InputNumber min={0} max={10_000} className="full-width-control" />
              </Form.Item>
              <Form.Item label="小时" name="hours">
                <InputNumber min={0} max={10_000} className="full-width-control" />
              </Form.Item>
              <Form.Item label="天" name="days">
                <InputNumber min={0} max={10_000} className="full-width-control" />
              </Form.Item>
              <Form.Item label="周" name="weeks">
                <InputNumber min={0} max={10_000} className="full-width-control" />
              </Form.Item>
            </div>
          </Form.Item>
          <div className="two-column-form">
            <Form.Item
              label="开始时间（可选，ISO 8601）"
              name="start_at"
              tooltip="留空表示立即开始；例如 2026-08-20T08:00:00Z"
            >
              <Input placeholder="2026-08-20T08:00:00Z" />
            </Form.Item>
            <Form.Item
              label="结束时间（可选，ISO 8601）"
              name="end_at"
              tooltip="留空表示不自动结束"
            >
              <Input placeholder="2026-09-01T08:00:00Z" />
            </Form.Item>
          </div>
        </>
      )}

      {scheduleType === "one_time" && (
        <Form.Item
          label="执行时间（ISO 8601）"
          name="run_at"
          tooltip="核心按 UTC 解释无时区时间"
          rules={[{ required: true, message: "请输入一次性执行时间" }]}
        >
          <Input className="management-code-input" placeholder="2026-08-20T08:00:00Z" />
        </Form.Item>
      )}

      {(scheduleType === "cron" || scheduleType === "interval") && (
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
      )}

      {scheduleType !== "one_time" && (
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
      )}

      {scheduleType === "one_time" && (
        <Form.Item
          label={editing ? "启用任务" : "创建后启用"}
          name="enabled"
          valuePropName="checked"
        >
          <Switch checkedChildren="启用" unCheckedChildren="停用" />
        </Form.Item>
      )}

      <Alert
        className="form-context-alert"
        type="info"
        showIcon
        message="调度定义会持久化保存"
        description={
          <Typography.Text type="secondary">
            服务重启时由核心重新恢复计时器。轮询动作只负责读取 Trigger Binding；发布动作只生成 schedule.tick 事件。
          </Typography.Text>
        }
      />
    </>
  );
}
