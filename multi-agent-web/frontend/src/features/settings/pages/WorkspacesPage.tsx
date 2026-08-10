import { DatabaseOutlined, SafetyCertificateOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Card, Empty, List, Tag, Typography } from "antd";
import { coreApi } from "../../../shared/api/client";

export function WorkspacesPage() {
  const query = useQuery({ queryKey: ["workspaces"], queryFn: coreApi.workspaces });
  const entries = Object.entries(query.data ?? {});
  return (
    <div className="page list-page">
      <div className="page-heading">
        <div>
          <div className="page-kicker"><DatabaseOutlined /> 设置</div>
          <Typography.Title level={2}>授权工作区</Typography.Title>
          <Typography.Paragraph type="secondary">
            浏览器只能提交工作区 ID；真实路径和准入边界始终由核心服务维护。
          </Typography.Paragraph>
        </div>
      </div>
      <Alert
        type="info"
        showIcon
        icon={<SafetyCertificateOutlined />}
        message="服务端工作区白名单"
        description="前端不接受任意 cwd，也不会读取或修改核心服务的本地配置。"
      />
      <Card className="workspace-card">
        {entries.length ? (
          <List
            dataSource={entries}
            renderItem={([id, path]) => (
              <List.Item extra={<Tag color="success">已授权</Tag>}>
                <List.Item.Meta
                  avatar={<span className="workspace-avatar"><DatabaseOutlined /></span>}
                  title={id}
                  description={<Typography.Text code copyable>{path}</Typography.Text>}
                />
              </List.Item>
            )}
          />
        ) : (
          <Empty description={query.isLoading ? "正在读取工作区" : "没有已授权工作区"} />
        )}
      </Card>
    </div>
  );
}
