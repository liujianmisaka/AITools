import { ApartmentOutlined, ArrowRightOutlined, PlusOutlined } from "@ant-design/icons";
import { Button, Card, Col, Empty, Row, Space, Tag, Typography } from "antd";
import { useNavigate } from "react-router-dom";
import { useWorkflowStore } from "../model/store";

export function WorkflowListPage() {
  const navigate = useNavigate();
  const tasks = useWorkflowStore((state) => state.tasks);
  const workflowName = useWorkflowStore((state) => state.workflowName);

  return (
    <div className="page list-page">
      <div className="page-heading">
        <div>
          <div className="page-kicker"><ApartmentOutlined /> 工作流</div>
          <Typography.Title level={2}>工作流空间</Typography.Title>
          <Typography.Paragraph type="secondary">
            当前阶段保留一个本地编排草稿；服务端工作流持久化将在下一阶段接入。
          </Typography.Paragraph>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate("/workflows/new")}>
          新建工作流
        </Button>
      </div>
      <Row gutter={[18, 18]}>
        {tasks.length > 0 && (
          <Col xs={24} lg={12} xl={8}>
            <Card className="workflow-list-card" hoverable onClick={() => navigate("/workflows/new")}>
              <div className="card-icon"><ApartmentOutlined /></div>
              <Space direction="vertical" size={8}>
                <Tag color="processing" bordered={false}>本地草稿</Tag>
                <Typography.Title level={4}>{workflowName}</Typography.Title>
                <Typography.Text type="secondary">{tasks.length} 个任务节点，等待校验或执行。</Typography.Text>
                <Button type="link" className="inline-link" icon={<ArrowRightOutlined />} iconPosition="end">
                  继续编排
                </Button>
              </Space>
            </Card>
          </Col>
        )}
        <Col xs={24} lg={12} xl={8}>
          <Card className="workflow-list-card template-card" onClick={() => navigate("/workflows/new")}>
            <div className="card-icon muted"><PlusOutlined /></div>
            <Typography.Title level={4}>从空白画布开始</Typography.Title>
            <Typography.Text type="secondary">
              使用任务弹窗逐个创建节点，再通过右侧参数面板补充执行契约。
            </Typography.Text>
          </Card>
        </Col>
      </Row>
      {!tasks.length && (
        <div className="list-empty">
          <Empty description="还没有本地工作流草稿" />
        </div>
      )}
    </div>
  );
}
