import {
  ApartmentOutlined,
  ArrowRightOutlined,
  ClockCircleOutlined,
  DeleteOutlined,
  PlusOutlined,
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
} from "antd";
import { useNavigate } from "react-router-dom";
import { coreApi } from "../../../shared/api/client";
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
    <div className="page list-page">
      <div className="page-heading">
        <div>
          <div className="page-kicker"><ApartmentOutlined /> 工作流模板</div>
          <Typography.Title level={2}>模板库</Typography.Title>
          <Typography.Paragraph type="secondary">
            模板是可编辑、可版本化的任务编排定义；每次执行都会创建独立实例快照。
          </Typography.Paragraph>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={createNew}>
          新建模板
        </Button>
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
