import { create } from "zustand";
import type { FailurePolicy, ProviderDescription, TaskDraft } from "../../../shared/types";
import { createTaskDraft } from "../../../shared/lib/workflow";

interface WorkflowState {
  workflowName: string;
  maxConcurrency: number;
  failurePolicy: FailurePolicy;
  tasks: TaskDraft[];
  selectedTaskId: string | null;
  createModalOpen: boolean;
  settingsModalOpen: boolean;
  setWorkflowName: (name: string) => void;
  setWorkflowSettings: (values: {
    maxConcurrency: number;
    failurePolicy: FailurePolicy;
  }) => void;
  setCreateModalOpen: (open: boolean) => void;
  setSettingsModalOpen: (open: boolean) => void;
  selectTask: (taskId: string | null) => void;
  addTask: (task: TaskDraft) => void;
  updateTask: (originalId: string, task: TaskDraft) => void;
  removeTask: (taskId: string) => void;
  duplicateTask: (taskId: string) => void;
  loadAdditionSample: (providers: ProviderDescription[], workspaceIds: string[]) => void;
}

function nextTaskId(tasks: TaskDraft[], prefix = "task"): string {
  const known = new Set(tasks.map((task) => task.id));
  let index = 1;
  while (known.has(`${prefix}_${index}`)) index += 1;
  return `${prefix}_${index}`;
}

export const useWorkflowStore = create<WorkflowState>((set, get) => ({
  workflowName: "新建 Agent 工作流",
  maxConcurrency: 2,
  failurePolicy: "continue_independent",
  tasks: [],
  selectedTaskId: null,
  createModalOpen: false,
  settingsModalOpen: false,
  setWorkflowName: (workflowName) => set({ workflowName }),
  setWorkflowSettings: ({ maxConcurrency, failurePolicy }) =>
    set({ maxConcurrency, failurePolicy }),
  setCreateModalOpen: (createModalOpen) => set({ createModalOpen }),
  setSettingsModalOpen: (settingsModalOpen) => set({ settingsModalOpen }),
  selectTask: (selectedTaskId) => set({ selectedTaskId }),
  addTask: (task) =>
    set((state) => ({
      tasks: [...state.tasks, task],
      selectedTaskId: task.id,
      createModalOpen: false,
    })),
  updateTask: (originalId, task) =>
    set((state) => ({
      tasks: state.tasks.map((candidate) => {
        if (candidate.id === originalId) return task;
        if (originalId !== task.id && candidate.depends_on.includes(originalId)) {
          return {
            ...candidate,
            depends_on: candidate.depends_on.map((id) => (id === originalId ? task.id : id)),
          };
        }
        return candidate;
      }),
      selectedTaskId: task.id,
    })),
  removeTask: (taskId) =>
    set((state) => ({
      tasks: state.tasks
        .filter((task) => task.id !== taskId)
        .map((task) => ({
          ...task,
          depends_on: task.depends_on.filter((dependency) => dependency !== taskId),
        })),
      selectedTaskId: state.selectedTaskId === taskId ? null : state.selectedTaskId,
    })),
  duplicateTask: (taskId) => {
    const state = get();
    const task = state.tasks.find((candidate) => candidate.id === taskId);
    if (!task) return;
    const copy = { ...task, id: nextTaskId(state.tasks, `${task.id}_copy`) };
    set({ tasks: [...state.tasks, copy], selectedTaskId: copy.id });
  },
  loadAdditionSample: (providers, workspaceIds) => {
    const first = createTaskDraft(providers, workspaceIds, "extract_formulas");
    const second = createTaskDraft(providers, workspaceIds, "calculate_results");
    first.role = "formula_reader";
    first.prompt_template =
      "读取工作区 multi-agent/examples/addition_pipeline/inputs/ 下所有 .txt 文件。每个文件是一条整数加法公式。不要计算，只按文件名排序并输出公式 JSON。";
    first.output_schema_text = JSON.stringify(
      {
        type: "object",
        required: ["formulas"],
        properties: {
          formulas: {
            type: "array",
            items: {
              type: "object",
              required: ["source", "expression"],
              properties: {
                source: { type: "string" },
                expression: { type: "string" },
              },
              additionalProperties: false,
            },
          },
        },
        additionalProperties: false,
      },
      null,
      2,
    );
    second.role = "calculator";
    second.depends_on = ["extract_formulas"];
    second.prompt_template =
      "读取下面的上游任务输出，逐条计算加法并输出结果 JSON。\n{{tasks.extract_formulas.output}}";
    second.output_schema_text = JSON.stringify(
      {
        type: "object",
        required: ["results"],
        properties: {
          results: {
            type: "array",
            items: {
              type: "object",
              required: ["source", "expression", "result"],
              properties: {
                source: { type: "string" },
                expression: { type: "string" },
                result: { type: "integer" },
              },
              additionalProperties: false,
            },
          },
        },
        additionalProperties: false,
      },
      null,
      2,
    );
    set({
      workflowName: "两阶段加法流水线",
      maxConcurrency: 1,
      failurePolicy: "fail_fast",
      tasks: [first, second],
      selectedTaskId: first.id,
    });
  },
}));
