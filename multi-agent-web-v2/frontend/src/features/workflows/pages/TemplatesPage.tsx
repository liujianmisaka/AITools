import {
  CloudUploadOutlined,
  EditOutlined,
  PlayCircleOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Modal, Space, Table, Typography, Upload } from "antd";
import type { UploadProps } from "antd";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../../shared/api/client";
import type { TemplateRecord } from "../../../shared/types";
import { PageHeader } from "../../../shared/ui/PageHeader";
import { WorkflowInputEditor } from "../components/WorkflowInputEditor";
import {
  formatWorkflowInputExample,
  nextVersion,
  parseWorkflowFile,
  parseWorkflowInput,
} from "../model/workflow";

const { Dragger } = Upload;

export function TemplatesPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [runTemplate, setRunTemplate] = useState<TemplateRecord | null>(null);
  const [workflowInput, setWorkflowInput] = useState("{}");
  const templates = useQuery({ queryKey: ["templates"], queryFn: api.listTemplates });
  const runVersion = useQuery({
    queryKey: ["template-version", runTemplate?.templateId, runTemplate?.latestVersion],
    queryFn: () =>
      api.getTemplateVersion(runTemplate!.templateId, runTemplate!.latestVersion),
    enabled: Boolean(runTemplate),
  });

  useEffect(() => {
    const schema = runVersion.data?.definition.spec.inputSchema;
    if (runTemplate && schema) setWorkflowInput(formatWorkflowInputExample(schema));
  }, [runTemplate, runVersion.data]);

  const run = useMutation({
    mutationFn: async () => {
      if (!runTemplate) throw new Error("请选择要运行的工作流模板");
      return api.startInstance(
        runTemplate.templateId,
        runTemplate.latestVersion,
        parseWorkflowInput(workflowInput),
      );
    },
    onSuccess: async (instance) => {
      setRunTemplate(null);
      await queryClient.invalidateQueries({ queryKey: ["instances"] });
      navigate(`/instances/${encodeURIComponent(instance.instanceId)}`);
    },
    onError: (error) =>
      message.error(error instanceof Error ? error.message : "启动失败"),
  });

  const openRun = (template: TemplateRecord) => {
    setWorkflowInput("{}");
    setRunTemplate(template);
  };

  const resetWorkflowInput = () => {
    const schema = runVersion.data?.definition.spec.inputSchema;
    if (schema) setWorkflowInput(formatWorkflowInputExample(schema));
  };

  const upload: UploadProps = {
    accept: ".json,application/json",
    multiple: false,
    showUploadList: false,
    beforeUpload: async (file) => {
      try {
        const document = nextVersion(parseWorkflowFile(await file.text()), 1);
        await api.createTemplate(document);
        await api.createTemplateVersion(document.metadata.id, document);
        await queryClient.invalidateQueries({ queryKey: ["templates"] });
        message.success(`模板 ${document.metadata.id} 已导入并持久化`);
        navigate(`/templates/${encodeURIComponent(document.metadata.id)}`);
      } catch (error) {
        message.error(error instanceof Error ? error.message : "导入失败");
      }
      return Upload.LIST_IGNORE;
    },
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="Workflow library"
        title="工作流模板"
        description="模板保存定义与编译结果；每个实例只引用不可变版本。"
        actions={
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => navigate("/templates/new")}
          >
            新建模板
          </Button>
        }
      />
      <Dragger {...upload} className="template-import">
        <p className="ant-upload-drag-icon"><CloudUploadOutlined /></p>
        <p className="ant-upload-text">打开或拖入单个 Workflow JSON</p>
        <p className="ant-upload-hint">
          文件会先经过结构检查，再通过 Control API 编译并持久化为模板版本 1。
        </p>
      </Dragger>
      <Card className="surface-card">
        <Table<TemplateRecord>
          rowKey="templateId"
          loading={templates.isLoading}
          dataSource={templates.data ?? []}
          pagination={false}
          columns={[
            {
              title: "模板",
              dataIndex: "name",
              render: (_, record) => (
                <div className="table-primary">
                  <Typography.Text strong>{record.name}</Typography.Text>
                  <Typography.Text type="secondary">{record.templateId}</Typography.Text>
                </div>
              ),
            },
            { title: "最新版本", dataIndex: "latestVersion", width: 120, render: (v) => `v${v}` },
            { title: "修订", dataIndex: "revision", width: 90 },
            {
              title: "更新时间",
              dataIndex: "updatedAt",
              width: 190,
              render: (value: string) => new Date(value).toLocaleString(),
            },
            {
              title: "",
              width: 210,
              render: (_, record) => (
                <Space>
                  <Button
                    type="primary"
                    icon={<PlayCircleOutlined />}
                    onClick={() => openRun(record)}
                  >
                    运行
                  </Button>
                  <Button
                    icon={<EditOutlined />}
                    onClick={() => navigate(`/templates/${encodeURIComponent(record.templateId)}`)}
                  >
                    打开
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>
      <Modal
        open={Boolean(runTemplate)}
        width={720}
        title={`运行 ${runTemplate?.name ?? "工作流模板"}`}
        okText={runTemplate ? `运行 v${runTemplate.latestVersion}` : "运行"}
        cancelText="取消"
        confirmLoading={run.isPending}
        okButtonProps={{ disabled: runVersion.isLoading || runVersion.isError }}
        onCancel={() => setRunTemplate(null)}
        onOk={() => run.mutate()}
      >
        <Typography.Paragraph type="secondary">
          将直接创建绑定最新版本的工作流实例。请按下方字段说明确认本次运行输入。
        </Typography.Paragraph>
        <WorkflowInputEditor
          schema={runVersion.data?.definition.spec.inputSchema}
          loading={runVersion.isLoading}
          error={runVersion.isError}
          value={workflowInput}
          onChange={setWorkflowInput}
          onReset={resetWorkflowInput}
        />
      </Modal>
    </div>
  );
}
