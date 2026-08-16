import { ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Descriptions, Space, Table, Tag, Typography } from "antd";
import { api } from "../../shared/api/client";
import { PageHeader } from "../../shared/ui/PageHeader";

export function CatalogPage() {
  const catalog = useQuery({ queryKey: ["catalog"], queryFn: api.getCatalog });
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: api.getWorkspaces });

  return (
    <div className="page">
      <PageHeader
        eyebrow="Runtime catalog"
        title="模型与工作区"
        description="目录由本地 Codex Runtime 官方 model/list 刷新；模板不能自由输入模型或本机路径。"
        actions={<Button icon={<ReloadOutlined />} onClick={() => void catalog.refetch()}>刷新</Button>}
      />
      <Card className="surface-card catalog-summary">
        <Descriptions column={{ xs: 1, md: 3 }}>
          <Descriptions.Item label="运行时">{catalog.data?.runtimeName ?? "未就绪"}</Descriptions.Item>
          <Descriptions.Item label="Provider">{catalog.data?.providerId ?? "未就绪"}</Descriptions.Item>
          <Descriptions.Item label="目录修订">
            <Typography.Text copyable ellipsis>{catalog.data?.revision ?? "未就绪"}</Typography.Text>
          </Descriptions.Item>
          <Descriptions.Item label="工作区" span={3}>
            <Space wrap>{(workspaces.data ?? []).map((item) => <Tag key={item}>{item}</Tag>)}</Space>
          </Descriptions.Item>
        </Descriptions>
      </Card>
      <Card className="surface-card">
        <Table
          rowKey="id"
          dataSource={catalog.data?.models ?? []}
          pagination={false}
          columns={[
            {
              title: "模型",
              render: (_, record) => (
                <div className="table-primary">
                  <Typography.Text strong>{record.label}</Typography.Text>
                  <Typography.Text type="secondary">{record.id}</Typography.Text>
                </div>
              ),
            },
            { title: "类型", dataIndex: "modelType", width: 160 },
            {
              title: "推理等级",
              dataIndex: "efforts",
              render: (efforts: string[], record) => (
                <Space wrap>
                  {efforts.map((effort) => (
                    <Tag color={effort === record.recommendedEffort ? "blue" : "default"} key={effort}>
                      {effort}
                    </Tag>
                  ))}
                </Space>
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
}
