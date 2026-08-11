import {
  ApartmentOutlined,
  ArrowRightOutlined,
  ClockCircleOutlined,
  DeleteOutlined,
  InboxOutlined,
  PlusOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  App,
  Button,
  Card,
  Col,
  Empty,
  Popconfirm,
  Row,
  Space,
  Spin,
  Tag,
  Typography,
  Upload,
} from "antd";
import type { DragEvent } from "react";
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, coreApi } from "../../../shared/api/client";
import {
  forkImportedTemplate,
  readWorkflowTemplateFile,
} from "../../../shared/lib/workflowImport";
import type { WorkflowTemplateSummary } from "../../../shared/types";
import { useWorkflowStore } from "../model/store";

export function WorkflowListPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { message, modal } = App.useApp();
  const localTasks = useWorkflowStore((state) => state.tasks);
  const localName = useWorkflowStore((state) => state.workflowName);
  const localWorkflowId = useWorkflowStore((state) => state.workflowId);
  const dirty = useWorkflowStore((state) => state.dirty);
  const resetWorkflow = useWorkflowStore((state) => state.resetWorkflow);
  const [dragActive, setDragActive] = useState(false);
  const dragDepth = useRef(0);

  const templatesQuery = useInfiniteQuery({
    queryKey: ["templates"],
    queryFn: ({ pageParam }) =>
      coreApi.listTemplates({ limit: 24, cursor: pageParam || undefined }),
    initialPageParam: "",
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const templates = templatesQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const archiveMutation = useMutation({
    mutationFn: (templateId: string) => coreApi.archiveTemplate(templateId),
    onSuccess: (record) => {
      queryClient.invalidateQueries({ queryKey: ["templates"] });
      if (record.id === localWorkflowId) resetWorkflow();
      message.success(`已归档“${record.name}”`);
    },
    onError: (error: Error) => message.error(error.message),
  });
  const importMutation = useMutation({
    mutationFn: async ({ file, fork }: { file: File; fork?: boolean }) => {
      const imported = await readWorkflowTemplateFile(file);
      const definition = fork ? forkImportedTemplate(imported) : imported;
      const record = await coreApi.createTemplate(definition);
      return { fileName: file.name, record };
    },
    onSuccess: ({ fileName, record }) => {
      queryClient.invalidateQueries({ queryKey: ["templates"] });
      message.success(`已从 ${fileName} 导入并保存“${record.name}”`);
    },
    onError: (error: Error, request) => {
      if (
        error instanceof ApiError
        && error.code === "workflow_template_version_conflict"
        && !request.fork
      ) {
        modal.confirm({
          title: "模板 ID 已存在",
          content: "可以移除文件中的模板 ID，并作为新的模板副本导入。",
          okText: "另存为新模板",
          cancelText: "取消导入",
          onOk: () => importMutation.mutate({ ...request, fork: true }),
        });
        return;
      }
      message.error(error.message);
    },
  });

  const importFile = (file: File) => {
    importMutation.mutate({ file });
  };

  const handleDragEnter = (event: DragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer.types).includes("Files")) return;
    event.preventDefault();
    dragDepth.current += 1;
    setDragActive(true);
  };

  const handleDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer.types).includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setDragActive(true);
  };

  const handleDragLeave = () => {
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragActive(false);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    dragDepth.current = 0;
    setDragActive(false);
    const files = Array.from(event.dataTransfer.files);
    if (files.length !== 1) {
      message.error("请一次拖入一个工作流 JSON 文件");
      return;
    }
    importFile(files[0]);
  };

  const discardThen = (action: () => void) => {
    if (!dirty) {
      action();
      return;
    }
    modal.confirm({
      title: "放弃未保存的修改？",
      content: "当前画布的未保存内容不会写入服务端。",
      okText: "放弃并继续",
      cancelText: "返回",
      okButtonProps: { danger: true },
      onOk: action,
    });
  };

  const createNew = () => {
    discardThen(() => {
      resetWorkflow();
      navigate("/templates/new");
    });
  };

  const openTemplate = (templateId: string) => {
    if (!dirty || templateId === localWorkflowId) {
      navigate(`/templates/${templateId}`);
      return;
    }
    discardThen(() => {
      resetWorkflow();
      navigate(`/templates/${templateId}`);
    });
  };

  const templateCard = (template: WorkflowTemplateSummary) => (
    <Col xs={24} md={12} xl={8} key={template.id}>
      <Card
        className="workflow-list-card persisted-card"
        hoverable
        onClick={() => openTemplate(template.id)}
      >
        <div className="workflow-card-topline">
          <div className="card-icon"><ApartmentOutlined /></div>
          <Popconfirm
            title="归档工作流模板？"
            description="历史运行不会被删除。"
            okText="归档"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={(event) => {
              event?.stopPropagation();
              archiveMutation.mutate(template.id);
            }}
          >
            <Button
              type="text"
              danger
              icon={<DeleteOutlined />}
              aria-label={`归档 ${template.name}`}
              onClick={(event) => event.stopPropagation()}
            />
          </Popconfirm>
        </div>
        <Space direction="vertical" size={8} className="workflow-card-copy">
          <Space size={6} wrap>
            <Tag color="processing" bordered={false}>版本 {template.version}</Tag>
            <Tag bordered={false}>{template.task_count} 个任务</Tag>
          </Space>
          <Typography.Title level={4}>{template.name}</Typography.Title>
          <Typography.Text type="secondary" className="workflow-updated-at">
            <ClockCircleOutlined /> {new Date(template.updated_at).toLocaleString("zh-CN")}
          </Typography.Text>
          <Button type="link" className="inline-link" icon={<ArrowRightOutlined />} iconPosition="end">
            编辑模板
          </Button>
        </Space>
      </Card>
    </Col>
  );

  return (
    <div
      className={`page list-page template-library-page${dragActive ? " is-file-dragging" : ""}`}
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {dragActive && (
        <div className="template-import-overlay" role="status" aria-live="polite">
          <InboxOutlined />
          <strong>松开以导入工作流模板</strong>
          <span>仅支持单个 JSON 文件，导入后由核心服务校验并持久化</span>
        </div>
      )}
      <div className="page-heading">
        <div>
          <div className="page-kicker"><ApartmentOutlined /> 工作流模板</div>
          <Typography.Title level={2}>模板库</Typography.Title>
          <Typography.Paragraph type="secondary">
            模板是可编辑、可版本化的任务编排定义；每次执行都会创建独立实例快照。
          </Typography.Paragraph>
        </div>
        <Space wrap>
          <Upload
            accept=".json,application/json"
            maxCount={1}
            showUploadList={false}
            disabled={importMutation.isPending}
            beforeUpload={(file) => {
              importFile(file);
              return Upload.LIST_IGNORE;
            }}
          >
            <Button
              icon={<UploadOutlined />}
              loading={importMutation.isPending}
            >
              打开 JSON 文件
            </Button>
          </Upload>
          <Button type="primary" icon={<PlusOutlined />} onClick={createNew}>
            新建模板
          </Button>
        </Space>
      </div>

      {dirty && localTasks.length > 0 && (
        <div className="local-draft-banner">
          <div>
            <Tag color="warning" bordered={false}>未保存草稿</Tag>
            <strong>{localName}</strong>
            <span>{localTasks.length} 个任务节点</span>
          </div>
          <Button onClick={() => navigate(localWorkflowId ? `/templates/${localWorkflowId}` : "/templates/new")}>
            继续编辑
          </Button>
        </div>
      )}

      {templatesQuery.isLoading ? (
        <div className="centered-state"><Spin size="large" tip="正在加载模板" /></div>
      ) : templates.length ? (
        <>
          <Row gutter={[18, 18]}>{templates.map(templateCard)}</Row>
          {templatesQuery.hasNextPage && (
            <div className="load-more-row">
              <Button
                loading={templatesQuery.isFetchingNextPage}
                onClick={() => templatesQuery.fetchNextPage()}
              >
                加载更多
              </Button>
            </div>
          )}
        </>
      ) : (
        <div className="list-empty">
          <Empty
            description="还没有已保存的工作流模板"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          >
            <Button type="primary" icon={<PlusOutlined />} onClick={createNew}>
              创建第一个模板
            </Button>
          </Empty>
        </div>
      )}
    </div>
  );
}
