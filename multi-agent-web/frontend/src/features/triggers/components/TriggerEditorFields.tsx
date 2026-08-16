import { Alert, Collapse, Form, Input, InputNumber, Select, Switch } from "antd";
import { useEffect, useMemo } from "react";
import type {
  EventSourceDescription,
  EventTypeDescription,
  WorkflowTemplateSummary,
  WorkspaceMap,
} from "../../../shared/types";
import type { TriggerEditorValues } from "../model/triggerForm";

interface TriggerEditorFieldsProps {
  form: ReturnType<typeof Form.useForm<TriggerEditorValues>>[0];
  sources: EventSourceDescription[];
  eventTypes: EventTypeDescription[];
  templates: WorkflowTemplateSummary[];
  workspaces: WorkspaceMap;
  editing?: boolean;
}

export function TriggerEditorFields({
  form,
  sources,
  eventTypes,
  templates,
  workspaces,
  editing = false,
}: TriggerEditorFieldsProps) {
  const sourceType = Form.useWatch("source_type", form);
  const eventType = Form.useWatch("event_type", form);
  const allowedEventTypes = useMemo(
    () => eventTypes.filter((item) => item.source_types.includes(sourceType)),
    [eventTypes, sourceType],
  );
  const selectedEvent = allowedEventTypes.find(
    (item) => item.event_type === eventType,
  );

  useEffect(() => {
    if (
      sourceType
      && allowedEventTypes.length
      && !allowedEventTypes.some((item) => item.event_type === eventType)
    ) {
      form.setFieldsValue({
        event_type: allowedEventTypes[0].event_type,
        event_version: allowedEventTypes[0].version,
      });
    }
  }, [allowedEventTypes, eventType, form, sourceType]);

  return (
    <>
      <div className="two-column-form">
        <Form.Item
          label="规则 ID"
          name="id"
          rules={[
            { required: true, message: "请输入规则 ID" },
            {
              pattern: /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/,
              message: "仅支持字母、数字、点、下划线和连字符",
            },
          ]}
        >
          <Input disabled={editing} />
        </Form.Item>
        <Form.Item
          label="规则名称"
          name="name"
          rules={[{ required: true, message: "请输入规则名称" }]}
        >
          <Input placeholder="例如：监控主分支提交" />
        </Form.Item>
      </div>

      <div className="two-column-form">
        <Form.Item
          label="事件源"
          name="source_type"
          rules={[{ required: true }]}
        >
          <Select
            disabled={editing}
            options={sources.map((source) => ({
              value: source.source_type,
              label:
                source.source_type === "git_commit"
                  ? "Git 分支提交"
                  : source.source_type === "manual"
                    ? "手动事件"
                    : source.source_type === "webhook"
                      ? "Generic Webhook"
                      : source.source_type === "schedule"
                        ? "计划合成事件"
                        : source.source_type === "internal"
                          ? "内部系统事件"
                          : source.source_type,
            }))}
          />
        </Form.Item>
        <Form.Item
          label="事件类型"
          name="event_type"
          rules={[{ required: true }]}
        >
          <Select
            onChange={(value) => {
              const definition = allowedEventTypes.find(
                (item) => item.event_type === value,
              );
              if (definition) {
                form.setFieldValue("event_version", definition.version);
              }
            }}
            options={allowedEventTypes.map((item) => ({
              value: item.event_type,
              label: `${item.event_type}@${item.version}`,
            }))}
          />
        </Form.Item>
      </div>

      {selectedEvent && (
        <Alert
          className="form-context-alert"
          type="info"
          showIcon
          message={selectedEvent.description}
        />
      )}

      <Form.Item name="event_version" hidden>
        <InputNumber />
      </Form.Item>

      <Form.Item
        label="目标工作流模板"
        name="template_id"
        rules={[{ required: true, message: "请选择触发后运行的模板" }]}
      >
        <Select
          showSearch
          optionFilterProp="label"
          placeholder="选择已持久化的工作流模板"
          options={templates.map((template) => ({
            value: template.id,
            label: `${template.name} · v${template.version} · ${template.id}`,
          }))}
        />
      </Form.Item>

      {sourceType === "git_commit" ? (
        <>
          <div className="three-column-form trigger-source-grid">
            <Form.Item
              label="工作区"
              name="workspace_id"
              rules={[{ required: true, message: "请选择工作区" }]}
            >
              <Select
                placeholder="服务端白名单 ID"
                options={Object.keys(workspaces).map((id) => ({
                  value: id,
                  label: id,
                }))}
              />
            </Form.Item>
            <Form.Item
              label="Remote"
              name="remote"
              rules={[{ required: true, message: "请输入 remote" }]}
            >
              <Input placeholder="origin" />
            </Form.Item>
            <Form.Item
              label="分支"
              name="branch"
              rules={[{ required: true, message: "请输入分支名" }]}
            >
              <Input placeholder="main" />
            </Form.Item>
          </div>
          <Form.Item
            label="轮询时执行 fetch"
            name="fetch"
            valuePropName="checked"
          >
            <Switch checkedChildren="开启" unCheckedChildren="关闭" />
          </Form.Item>
          <Alert
            className="form-context-alert"
            type="warning"
            showIcon
            message="首次轮询只建立当前分支基线，不补发历史提交。"
          />
        </>
      ) : sourceType === "webhook" ? (
        <>
          <div className="two-column-form">
            <Form.Item
              label="Endpoint Key"
              name="endpoint_key"
              tooltip="同时也是 Trigger Binding 的 source_key"
              rules={[
                { required: true, message: "请输入 endpoint_key" },
                {
                  pattern: /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/,
                  message: "仅支持字母、数字、点、下划线和连字符",
                },
              ]}
            >
              <Input placeholder="例如：github-repo-a" />
            </Form.Item>
            <Form.Item
              label="密钥引用"
              name="secret_ref"
              tooltip="环境变量名，或 MULTI_AGENT_WEBHOOK_SECRET_<NAME> 中的 NAME"
            >
              <Input placeholder="例如：github-repo-a" />
            </Form.Item>
          </div>
          <div className="two-column-form">
            <Form.Item
              label="签名 Header"
              name="signature_header"
              rules={[{ required: true, message: "请输入签名 Header" }]}
            >
              <Input placeholder="x-hub-signature-256" />
            </Form.Item>
            <Form.Item label="签名算法" name="signature_algorithm">
              <Select
                options={[
                  { value: "sha256", label: "HMAC-SHA256" },
                  { value: "sha384", label: "HMAC-SHA384" },
                  { value: "sha512", label: "HMAC-SHA512" },
                ]}
              />
            </Form.Item>
          </div>
          <Form.Item
            label="要求签名"
            name="require_signature"
            valuePropName="checked"
          >
            <Switch checkedChildren="必须签名" unCheckedChildren="允许未签名" />
          </Form.Item>
          <div className="two-column-form">
            <Form.Item
              label="IP 白名单（CIDR，逗号分隔）"
              name="allowed_ip_cidrs"
              tooltip="留空表示不限制来源 IP"
            >
              <Input placeholder="127.0.0.1/32, 10.0.0.0/8" />
            </Form.Item>
            <Form.Item
              label="Payload 上限（字节）"
              name="max_payload_bytes"
              rules={[{ required: true, message: "请输入 payload 上限" }]}
            >
              <InputNumber min={1} max={10_485_760} className="full-width-control" />
            </Form.Item>
          </div>
          <div className="two-column-form">
            <Form.Item
              label="去重 Header"
              name="dedup_header"
              tooltip="留空表示不使用 Header 去重"
            >
              <Input placeholder="x-event-key" />
            </Form.Item>
            <Form.Item
              label="Body 去重窗口（秒）"
              name="dedup_window_seconds"
              tooltip="0 表示永久使用规范化 payload hash"
            >
              <InputNumber min={0} max={31_536_000} className="full-width-control" />
            </Form.Item>
          </div>
          <Alert
            className="form-context-alert"
            type="info"
            showIcon
            message={`Webhook 入口为 POST /api/v1/hooks/webhook/{endpoint_key}`}
          />
        </>
      ) : (
        <Form.Item
          label="来源键（可选）"
          name="source_key"
          tooltip="填写后只匹配使用相同 source_key 的事件"
        >
          <Input placeholder="例如：release-gate" />
        </Form.Item>
      )}

      <Collapse
        className="advanced-form-collapse"
        ghost
        items={[
          {
            key: "advanced",
            label: "高级匹配与输入映射",
            children: (
              <>
                <Form.Item
                  label="事件过滤 JSON"
                  name="event_filter_text"
                  tooltip="键为 payload 路径，值为期望的精确值"
                >
                  <Input.TextArea
                    rows={5}
                    spellCheck={false}
                    className="json-editor"
                    placeholder={'{\n  "branch": "main"\n}'}
                  />
                </Form.Item>
                <Form.Item
                  label="模板输入映射 JSON"
                  name="input_mapping_text"
                  tooltip='键为模板输入名，值为 payload 路径；空对象表示传入完整 payload'
                >
                  <Input.TextArea
                    rows={5}
                    spellCheck={false}
                    className="json-editor"
                    placeholder={'{\n  "sha": "payload.after_sha"\n}'}
                  />
                </Form.Item>
                <Form.Item
                  label="并发准入策略"
                  name="concurrency_policy"
                >
                  <Select
                    options={[
                      { value: "allow_parallel", label: "允许并行实例" },
                      {
                        value: "skip_if_running",
                        label: "模板已有运行实例时跳过",
                      },
                    ]}
                  />
                </Form.Item>
              </>
            ),
          },
        ]}
      />

      <Form.Item
        label={editing ? "启用规则" : "创建后启用"}
        name="enabled"
        valuePropName="checked"
      >
        <Switch checkedChildren="启用" unCheckedChildren="停用" />
      </Form.Item>
    </>
  );
}
