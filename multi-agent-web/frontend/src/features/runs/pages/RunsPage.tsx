import { CloudServerOutlined, SearchOutlined } from "@ant-design/icons";
import { Button, Empty, Input, Space, Typography } from "antd";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

export function RunsPage() {
  const navigate = useNavigate();
  const [runId, setRunId] = useState("");
  const openRun = () => {
    const value = runId.trim();
    if (value) navigate(`/runs/${encodeURIComponent(value)}`);
  };

  return (
    <div className="page list-page">
      <div className="page-heading">
        <div>
          <div className="page-kicker"><CloudServerOutlined /> 执行记录</div>
          <Typography.Title level={2}>运行中心</Typography.Title>
          <Typography.Paragraph type="secondary">
            查看工作流的实时状态、节点输出、错误和 Provider 会话信息。
          </Typography.Paragraph>
        </div>
      </div>
      <div className="run-lookup-card">
        <Space.Compact block>
          <Input
            size="large"
            value={runId}
            onChange={(event) => setRunId(event.target.value)}
            onPressEnter={openRun}
            placeholder="输入 Run ID 打开执行详情"
            prefix={<SearchOutlined />}
          />
          <Button type="primary" size="large" onClick={openRun} disabled={!runId.trim()}>
            打开运行
          </Button>
        </Space.Compact>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="核心服务暂未提供运行列表接口；新提交的工作流会自动跳转到详情页。"
        />
      </div>
    </div>
  );
}
