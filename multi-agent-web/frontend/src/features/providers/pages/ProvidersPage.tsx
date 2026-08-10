import { DatabaseOutlined, ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Descriptions, Empty, Row, Space, Table, Tag, Typography } from "antd";
import { coreApi } from "../../../shared/api/client";
import type { ProviderModel } from "../../../shared/types";

export function ProvidersPage() {
  const query = useQuery({ queryKey: ["providers"], queryFn: coreApi.providers });
  const providers = query.data ?? [];
  return (
    <div className="page list-page">
      <div className="page-heading">
        <div>
          <div className="page-kicker"><DatabaseOutlined /> Provider 与模型</div>
          <Typography.Title level={2}>执行目录</Typography.Title>
          <Typography.Paragraph type="secondary">
            此页面只展示核心服务发布的目录。Codex 模型来自当前 OpenCodex 配置，不维护前端自定义清单。
          </Typography.Paragraph>
        </div>
        <Button icon={<ReloadOutlined />} loading={query.isFetching} onClick={() => query.refetch()}>
          刷新目录
        </Button>
      </div>
      {query.isError && <Alert type="error" showIcon message={(query.error as Error).message} />}
      {providers.length ? (
        <Row gutter={[18, 18]}>
          {providers.map((provider) => (
            <Col span={24} key={provider.name}>
              <Card
                className="provider-card"
                title={
                  <Space>
                    <span className="provider-avatar">{provider.name.slice(0, 2).toUpperCase()}</span>
                    <span>{provider.name}</span>
                    <Tag color={provider.available === false ? "error" : "success"} bordered={false}>
                      {provider.available === false ? "不可用" : "可用"}
                    </Tag>
                  </Space>
                }
              >
                {provider.error && (
                  <Alert
                    type="warning"
                    showIcon
                    message={provider.error.code ?? "目录读取失败"}
                    description={provider.error.message}
                  />
                )}
                <Descriptions size="small" column={{ xs: 1, md: 3 }} className="provider-descriptions">
                  <Descriptions.Item label="模型来源">
                    {String(provider.metadata?.model_catalog ?? "provider")}
                  </Descriptions.Item>
                  <Descriptions.Item label="模型数">{provider.models.length}</Descriptions.Item>
                  <Descriptions.Item label="写入能力">
                    {provider.capabilities.workspace_write_mode ? "支持" : "不支持"}
                  </Descriptions.Item>
                </Descriptions>
                <Table<ProviderModel>
                  rowKey="id"
                  size="middle"
                  pagination={false}
                  dataSource={provider.models}
                  locale={{ emptyText: "该 Provider 没有发布可选模型" }}
                  columns={[
                    { title: "模型", dataIndex: "label", render: (value, row) => (
                      <div className="model-cell"><strong>{String(value)}</strong><code>{row.id}</code></div>
                    ) },
                    { title: "类型", dataIndex: "model_type", width: 160, render: (value) => <Tag>{String(value)}</Tag> },
                    { title: "推理等级", dataIndex: "efforts", render: (values: string[]) => (
                      <Space size={[4, 4]} wrap>{values.map((value) => <Tag key={value} bordered={false}>{value}</Tag>)}</Space>
                    ) },
                  ]}
                />
              </Card>
            </Col>
          ))}
        </Row>
      ) : !query.isLoading ? (
        <Empty description="核心服务没有发布 Provider" />
      ) : null}
    </div>
  );
}
