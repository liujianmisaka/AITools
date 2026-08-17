import {
  CloudUploadOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  SaveOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App,
  Button,
  Input,
  InputNumber,
  Modal,
  Radio,
  Segmented,
  Space,
  Tag,
  Typography,
} from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../../../shared/api/client";
import type { JsonObject, WorkflowDocument, WorkflowNode } from "../../../shared/types";
import { NodeCreateModal } from "../components/NodeCreateModal";
import { NodeInspectorDrawer } from "../components/NodeInspectorDrawer";
import { WorkflowCanvas } from "../components/WorkflowCanvas";
import { WorkflowInputEditor } from "../components/WorkflowInputEditor";
import { useWorkflowEditor } from "../model/store";
import {
  formatWorkflowInputExample,
  nextVersion,
  parseWorkflowFile,
  parseWorkflowInput,
  transitionFor,
} from "../model/workflow";

export function WorkflowEditorPage() {
  const { templateId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { message, modal } = App.useApp();
  const fileInput = useRef<HTMLInputElement>(null);
  const loadedTemplate = useRef<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [runOpen, setRunOpen] = useState(false);
  const [workflowInput, setWorkflowInput] = useState("{}");
  const [dragging, setDragging] = useState(false);
  const {
    document,
    selectedNodeId,
    persisted,
    latestVersion,
    dirty,
    reset,
    load,
    selectNode,
    updateMetadata,
    updateSpec,
    addNode,
    updateNode,
    removeNode,
    markSaved,
  } = useWorkflowEditor();
  const catalog = useQuery({ queryKey: ["catalog"], queryFn: api.getCatalog, retry: 1 });
  const workspaces = useQuery({
    queryKey: ["workspaces"],
    queryFn: api.getWorkspaces,
    retry: 1,
  });
  const template = useQuery({
    queryKey: ["template", templateId],
    queryFn: () => api.getTemplate(templateId!),
    enabled: Boolean(templateId),
  });
  const version = useQuery({
    queryKey: ["template-version", templateId, template.data?.latestVersion],
    queryFn: () => api.getTemplateVersion(templateId!, template.data!.latestVersion),
    enabled: Boolean(templateId && template.data?.latestVersion),
  });

  useEffect(() => {
    if (!templateId) {
      if (loadedTemplate.current !== "__new__") {
        reset();
        loadedTemplate.current = "__new__";
      }
      return;
    }
    if (version.data && loadedTemplate.current !== templateId) {
      load(version.data.definition, true, version.data.version);
      loadedTemplate.current = templateId;
    }
  }, [load, reset, templateId, version.data]);

  const models = catalog.data?.models ?? [];
  const selectedNode =
    document.spec.nodes.find((node) => node.id === selectedNodeId) ?? null;

  const save = useMutation({
    mutationFn: async (): Promise<{ document: WorkflowDocument; version: number }> => {
      let currentLatest = latestVersion;
      if (!persisted) {
        await api.createTemplate(document);
        currentLatest = 0;
      }
      const versioned = nextVersion(document, currentLatest + 1);
      const saved = await api.createTemplateVersion(versioned.metadata.id, versioned);
      return { document: saved.definition, version: saved.version };
    },
    onSuccess: async ({ document: saved, version: savedVersion }) => {
      load(saved, true, savedVersion);
      markSaved(savedVersion);
      loadedTemplate.current = saved.metadata.id;
      await queryClient.invalidateQueries({ queryKey: ["templates"] });
      message.success(`模板版本 v${savedVersion} 已持久化`);
      if (!templateId) navigate(`/templates/${encodeURIComponent(saved.metadata.id)}`, { replace: true });
    },
    onError: (error) => message.error(error instanceof Error ? error.message : "保存失败"),
  });

  const run = useMutation({
    mutationFn: async () => {
      let savedVersion = latestVersion;
      if (dirty || !persisted) {
        const saved = await save.mutateAsync();
        savedVersion = saved.version;
      }
      const input: JsonObject = parseWorkflowInput(workflowInput);
      return api.startInstance(document.metadata.id, savedVersion, input);
    },
    onSuccess: (instance) => {
      setRunOpen(false);
      navigate(`/instances/${instance.instanceId}`);
    },
    onError: (error) => message.error(error instanceof Error ? error.message : "启动失败"),
  });

  const importDocument = async (file: File) => {
    if (!file.name.toLowerCase().endsWith(".json")) {
      message.error("请选择单个 .json 工作流文件");
      return;
    }
    try {
      const parsed = parseWorkflowFile(await file.text());
      load(parsed, false, 0);
      loadedTemplate.current = "__import__";
      message.success("工作流已导入；保存后会创建持久化模板");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "导入失败");
    }
  };

  const replacePredecessors = (node: WorkflowNode, predecessors: string[]) => {
    const remaining = document.spec.transitions.filter((item) => item.to !== node.id);
    const additions = predecessors.map((from) =>
      transitionFor(from, node.id, [...remaining]),
    );
    updateNode(node.id, node);
    updateSpec({ transitions: [...remaining, ...additions] });
  };

  const deleteNode = (nodeId: string) => {
    modal.confirm({
      title: `删除节点 ${nodeId}？`,
      content: "该节点相关的转移边也会删除。",
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () => removeNode(nodeId),
    });
  };

  const resetWorkflowInput = () => {
    setWorkflowInput(formatWorkflowInputExample(document.spec.inputSchema));
  };

  return (
    <div
      className={`editor-page ${dragging ? "is-dragging" : ""}`}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        const [file] = Array.from(event.dataTransfer.files);
        if (file) void importDocument(file);
      }}
    >
      {dragging && (
        <div className="drop-overlay">
          <CloudUploadOutlined />
          <strong>释放以导入 Workflow JSON</strong>
          <span>导入后仍需通过后端编译并持久化</span>
        </div>
      )}
      <div className="editor-header">
        <div className="editor-title">
          <Space size={8}>
            <Tag color={persisted ? "blue" : "default"}>
              {persisted ? `已保存 v${latestVersion}` : "新模板"}
            </Tag>
            {dirty && <Tag color="gold">未保存修改</Tag>}
          </Space>
          <Input
            className="workflow-name-input"
            value={document.metadata.name}
            onChange={(event) => updateMetadata({ name: event.target.value })}
          />
          <Space className="metadata-line">
            <Input
              addonBefore="ID"
              value={document.metadata.id}
              disabled={persisted}
              onChange={(event) => updateMetadata({ id: event.target.value })}
            />
            <Segmented
              value={document.spec.flow.type}
              options={[
                { value: "dag", label: "DAG" },
                { value: "state_machine", label: "状态机" },
              ]}
              onChange={(value) =>
                updateSpec({ flow: { type: value as "dag" | "state_machine" } })
              }
            />
            <InputNumber
              min={1}
              max={128}
              value={document.spec.maxConcurrency}
              addonBefore="并发"
              onChange={(value) => value && updateSpec({ maxConcurrency: value })}
            />
          </Space>
        </div>
        <Space wrap>
          <input
            ref={fileInput}
            hidden
            type="file"
            accept=".json,application/json"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void importDocument(file);
              event.target.value = "";
            }}
          />
          <Button icon={<UploadOutlined />} onClick={() => fileInput.current?.click()}>
            导入 JSON
          </Button>
          <Button
            icon={<PlusOutlined />}
            onClick={() => setCreateOpen(true)}
            disabled={!models.length || !workspaces.data?.length}
          >
            添加节点
          </Button>
          <Button
            icon={<SaveOutlined />}
            loading={save.isPending}
            onClick={() => save.mutate()}
          >
            保存版本
          </Button>
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            onClick={() => {
              resetWorkflowInput();
              setRunOpen(true);
            }}
          >
            {dirty || !persisted ? "保存并运行" : "运行"}
          </Button>
        </Space>
      </div>
      <div className="editor-workspace">
        <div className="canvas-panel">
          <div className="canvas-toolbar">
            <Typography.Text type="secondary">
              {document.spec.nodes.length} 个节点 · {document.spec.transitions.length} 条转移
            </Typography.Text>
            <Radio.Group
              size="small"
              value={document.spec.failurePolicy}
              onChange={(event) => updateSpec({ failurePolicy: event.target.value })}
              options={[
                { value: "continue_independent", label: "独立分支继续" },
                { value: "fail_fast", label: "快速失败" },
              ]}
            />
          </div>
          <WorkflowCanvas
            document={document}
            selectedNodeId={selectedNodeId}
            onSelectNode={selectNode}
          />
        </div>
      </div>
      <NodeCreateModal
        open={createOpen}
        models={models}
        workspaces={workspaces.data ?? []}
        existingNodes={document.spec.nodes}
        onCancel={() => setCreateOpen(false)}
        onCreate={(node, predecessors) => {
          addNode(node);
          const additions = predecessors.map((from) =>
            transitionFor(from, node.id, document.spec.transitions),
          );
          updateSpec({ transitions: [...document.spec.transitions, ...additions] });
          setCreateOpen(false);
        }}
      />
      <NodeInspectorDrawer
        node={selectedNode}
        nodes={document.spec.nodes}
        transitions={document.spec.transitions}
        models={models}
        workspaces={workspaces.data ?? []}
        onClose={() => selectNode(null)}
        onChange={replacePredecessors}
        onDelete={deleteNode}
      />
      <Modal
        open={runOpen}
        width={720}
        title="创建工作流实例"
        okText={dirty || !persisted ? "保存并运行" : "运行"}
        cancelText="取消"
        confirmLoading={run.isPending}
        onCancel={() => setRunOpen(false)}
        onOk={() => run.mutate()}
      >
        <Typography.Paragraph type="secondary">
          实例会绑定不可变模板版本。请按下方字段说明确认本次运行输入。
        </Typography.Paragraph>
        <WorkflowInputEditor
          schema={document.spec.inputSchema}
          value={workflowInput}
          onChange={setWorkflowInput}
          onReset={resetWorkflowInput}
        />
      </Modal>
    </div>
  );
}
