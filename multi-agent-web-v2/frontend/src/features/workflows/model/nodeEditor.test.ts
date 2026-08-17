import { describe, expect, it } from "vitest";
import { createAgentNode } from "./workflow";
import {
  InvalidNodeOutputSchemaError,
  updateNodeFromInspector,
} from "./nodeEditor";

describe("node inspector contract", () => {
  it("applies visible fields even before collapsed fields are mounted", () => {
    const node = createAgentNode("agent-1", undefined, undefined);
    const updated = updateNodeFromInspector(node, {
      instruction: "updated instruction",
    });

    expect(updated.agent?.instruction).toBe("updated instruction");
    expect(updated.outputSchema).toEqual(node.outputSchema);
    expect(updated.agent?.retry).toEqual(node.agent?.retry);
  });

  it("accepts only JSON objects as the node output schema", () => {
    const node = createAgentNode("agent-1", undefined, undefined);

    expect(
      updateNodeFromInspector(node, {
        outputSchema: '{"type":"object","additionalProperties":false}',
      }).outputSchema,
    ).toEqual({ type: "object", additionalProperties: false });
    expect(() => updateNodeFromInspector(node, { outputSchema: "[]" })).toThrow(
      InvalidNodeOutputSchemaError,
    );
  });
});
