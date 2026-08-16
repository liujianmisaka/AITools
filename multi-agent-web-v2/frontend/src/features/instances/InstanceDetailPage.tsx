import { CloseCircleOutlined, CopyOutlined, LinkOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Col, Descriptions, Row, Space, Tabs, Typography } from "antd";
import { useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../../shared/api/client";
import { PageHeader } from "../../shared/ui/PageHeader";
import { StatusTag } from "../../shared/ui/StatusTag";
import { WorkflowCanvas } from "../workflows/components/WorkflowCanvas";

const terminal = new Set(["succeeded", "failed", "cancelled", "attention_required"]);

export function InstanceDetailPage() {
  const { instanceId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { message, modal } = App.useApp();
  const detail = useQuery({
    queryKey: ["instance", instanceId],
    queryFn: () => api.getInstance(instanceId),
    refetchOnMount: "always",
    refetchInterval: (query) =>
      query.state.data && !terminal.has(query.state.data.instance.status) ? 1500 : false,
  });
  const version = useQuery({
    queryKey: [
      "template-version",
      detail.data?.instance.templateId,
      detail.data?.instance.templateVersion,
    ],
    queryFn: () =>
      api.getTemplateVersion(
        detail.data!.instance.templateId,
        detail.data!.instance.templateVersion,
      ),
    enabled: Boolean(detail.data),
  });

  useEffect(() => {
    const events = new EventSource("/api/v2/stream");
    events.addEventListener("milestone", () => {
      void queryClient.invalidateQueries({ queryKey: ["instance", instanceId] });
      void queryClient.invalidateQueries({ queryKey: ["instances"] });
    });
    return () => events.close();
  }, [instanceId, queryClient]);

  const instance = detail.data?.instance;
  if (!instance) {
    return <div className="page loading-page">正在恢复实例投影…</div>;
  }

  return (
    <div className="page">
      <PageHeader
        eyebrow="Workflow instance"
        title={instance.templateId}
        description={instance.instanceId}
        actions={
          <Space>
            <Button
              icon={<CopyOutlined />}
              onClick={() => {
                void navigator.clipboard.writeText(instance.temporalWorkflowId);
                message.success("Temporal Workflow ID 已复制");
              }}
            >
              复制 Workflow ID
            </Button>
            {!terminal.has(instance.status) && (
              <Button
                danger
                icon={<CloseCircleOutlined />}
                onClick={() =>
                  modal.confirm({
                    title: "取消该工作流实例？",
                    content: "Temporal 会取消正在运行和等待中的节点。",
                    okText: "取消实例",
                    okButtonProps: { danger: true },
                    cancelText: "返回",
                    onOk: async () => {
                      await api.cancelInstance(instance.instanceId, "cancelled from Web console");
                      await detail.refetch();
                    },
                  })
                }
              >
                取消实例
              </Button>
            )}
          </Space>
        }
      />
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={17}>
          <Card className="surface-card instance-canvas">
            {version.data ? (
              <WorkflowCanvas
                document={version.data.definition}
                projections={detail.data?.nodes ?? []}
              />
            ) : (
              <div className="loading-page">正在加载不可变模板版本…</div>
            )}
          </Card>
        </Col>
        <Col xs={24} xl={7}>
          <Card className="surface-card">
            <Space direction="vertical" size={14} style={{ width: "100%" }}>
              <StatusTag status={instance.status} />
              <Descriptions column={1} size="small">
                <Descriptions.Item label="模板版本">v{instance.templateVersion}</Descriptions.Item>
                <Descriptions.Item label="投影版本">{instance.projectionVersion}</Descriptions.Item>
                <Descriptions.Item label="Temporal">
                  <Typography.Text copyable ellipsis>
                    {instance.temporalWorkflowId}
                  </Typography.Text>
                </Descriptions.Item>
                <Descriptions.Item label="Run ID">
                  {instance.temporalRunId ?? "等待首次投影"}
                </Descriptions.Item>
                <Descriptions.Item label="更新时间">
                  {new Date(instance.updatedAt).toLocaleString()}
                </Descriptions.Item>
              </Descriptions>
              <Button
                icon={<LinkOutlined />}
                onClick={() => navigate(`/templates/${instance.templateId}`)}
              >
                打开模板
              </Button>
            </Space>
          </Card>
        </Col>
      </Row>
      <Card className="surface-card detail-tabs">
        <Tabs
          items={[
            {
              key: "nodes",
              label: `节点 (${detail.data?.nodes.length ?? 0})`,
              children: (
                <div className="node-timeline">
                  {(detail.data?.nodes ?? []).map((node) => (
                    <div className="node-timeline__item" key={`${node.nodeId}:${node.activation}`}>
                      <div>
                        <Typography.Text strong>{node.nodeId}</Typography.Text>
                        <Typography.Text type="secondary">
                          activation {node.activation}
                        </Typography.Text>
                      </div>
                      <StatusTag status={node.status} />
                      <pre>{JSON.stringify(node.output ?? node.errorMessage ?? {}, null, 2)}</pre>
                    </div>
                  ))}
                </div>
              ),
            },
            {
              key: "output",
              label: "实例输出",
              children: <pre className="json-output">{JSON.stringify(instance.output ?? {}, null, 2)}</pre>,
            },
            {
              key: "input",
              label: "实例输入",
              children: <pre className="json-output">{JSON.stringify(instance.workflowInput, null, 2)}</pre>,
            },
          ]}
        />
      </Card>
    </div>
  );
}
