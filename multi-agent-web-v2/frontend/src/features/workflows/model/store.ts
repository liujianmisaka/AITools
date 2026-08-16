import { create } from "zustand";
import type { WorkflowDocument, WorkflowNode } from "../../../shared/types";
import { createWorkflow } from "./workflow";

interface WorkflowEditorState {
  document: WorkflowDocument;
  selectedNodeId: string | null;
  persisted: boolean;
  latestVersion: number;
  dirty: boolean;
  reset: () => void;
  load: (document: WorkflowDocument, persisted: boolean, latestVersion: number) => void;
  selectNode: (nodeId: string | null) => void;
  updateMetadata: (updates: Partial<WorkflowDocument["metadata"]>) => void;
  updateSpec: (updates: Partial<WorkflowDocument["spec"]>) => void;
  addNode: (node: WorkflowNode) => void;
  updateNode: (nodeId: string, node: WorkflowNode) => void;
  removeNode: (nodeId: string) => void;
  markSaved: (latestVersion: number) => void;
}

export const useWorkflowEditor = create<WorkflowEditorState>((set) => ({
  document: createWorkflow(),
  selectedNodeId: null,
  persisted: false,
  latestVersion: 0,
  dirty: false,
  reset: () =>
    set({
      document: createWorkflow(),
      selectedNodeId: null,
      persisted: false,
      latestVersion: 0,
      dirty: false,
    }),
  load: (document, persisted, latestVersion) =>
    set({ document, persisted, latestVersion, selectedNodeId: null, dirty: false }),
  selectNode: (selectedNodeId) => set({ selectedNodeId }),
  updateMetadata: (updates) =>
    set((state) => ({
      document: {
        ...state.document,
        metadata: { ...state.document.metadata, ...updates },
      },
      dirty: true,
    })),
  updateSpec: (updates) =>
    set((state) => ({
      document: {
        ...state.document,
        spec: { ...state.document.spec, ...updates },
      },
      dirty: true,
    })),
  addNode: (node) =>
    set((state) => ({
      document: {
        ...state.document,
        spec: {
          ...state.document.spec,
          nodes: [...state.document.spec.nodes, node],
        },
      },
      selectedNodeId: node.id,
      dirty: true,
    })),
  updateNode: (nodeId, node) =>
    set((state) => ({
      document: {
        ...state.document,
        spec: {
          ...state.document.spec,
          nodes: state.document.spec.nodes.map((item) => (item.id === nodeId ? node : item)),
        },
      },
      dirty: true,
    })),
  removeNode: (nodeId) =>
    set((state) => ({
      document: {
        ...state.document,
        spec: {
          ...state.document.spec,
          nodes: state.document.spec.nodes.filter((item) => item.id !== nodeId),
          transitions: state.document.spec.transitions.filter(
            (transition) => transition.from !== nodeId && transition.to !== nodeId,
          ),
        },
      },
      selectedNodeId: state.selectedNodeId === nodeId ? null : state.selectedNodeId,
      dirty: true,
    })),
  markSaved: (latestVersion) => set({ latestVersion, persisted: true, dirty: false }),
}));
