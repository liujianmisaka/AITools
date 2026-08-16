from __future__ import annotations

from datetime import timedelta

import networkx as nx
from pydantic import BaseModel, ConfigDict

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.workflow_dsl.canonical import canonical_json, sha256_text
from multi_agent_v2.packages.workflow_dsl.errors import (
    CompilationIssue,
    WorkflowCompilationError,
    issue,
)
from multi_agent_v2.packages.workflow_dsl.expressions import validate_expression
from multi_agent_v2.packages.workflow_dsl.ir import (
    ActivityExecutionIr,
    AgentExecutionIr,
    ApprovalExecutionIr,
    BindingIr,
    DecisionExecutionIr,
    EventWaitExecutionIr,
    ExecutablePlan,
    JoinExecutionIr,
    NodeIr,
    RetryIr,
    StrictSchemaIr,
    TimerExecutionIr,
    TransitionIr,
)
from multi_agent_v2.packages.workflow_dsl.models import (
    MAX_WORKFLOW_NODES,
    MAX_WORKFLOW_TRANSITIONS,
    ActivityNode,
    AgentNode,
    ApprovalNode,
    DecisionNode,
    EventWaitNode,
    JoinNode,
    NodeDefinition,
    RetryDefinition,
    StateMachineFlow,
    TimerNode,
    TransitionDefinition,
    WorkflowDefinition,
)
from multi_agent_v2.packages.workflow_dsl.strict_schema import validate_strict_schema

COMPILER_VERSION = "1.0.0"


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderModel(ContextModel):
    provider: str
    model: str
    efforts: tuple[str, ...]


class RegisteredActivity(ContextModel):
    name: str
    version: int
    output_schema: JsonObject


class CompilationContext(ContextModel):
    catalog_revision: str
    provider_models: tuple[ProviderModel, ...]
    workspace_ids: tuple[str, ...]
    activities: tuple[RegisteredActivity, ...]


_DECISION_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"result": {"type": "boolean"}},
    "required": ["result"],
    "additionalProperties": False,
}
_APPROVAL_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "commandId": {"type": "string"},
        "decision": {"type": "string", "enum": ["approved", "rejected"]},
        "operatorLabel": {"type": ["string", "null"]},
        "reason": {"type": ["string", "null"]},
    },
    "required": ["commandId", "decision", "operatorLabel", "reason"],
    "additionalProperties": False,
}
_TIMER_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"fired": {"type": "boolean"}},
    "required": ["fired"],
    "additionalProperties": False,
}
_JOIN_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"completed": {"type": "array", "items": {"type": "string"}}},
    "required": ["completed"],
    "additionalProperties": False,
}


def compile_workflow(
    definition: WorkflowDefinition,
    context: CompilationContext,
) -> ExecutablePlan:
    issues = _collect_issues(definition, context)
    if issues:
        raise WorkflowCompilationError(issues)

    input_schema = _schema_ir(definition.spec.input_schema)
    output_schema = _schema_ir(definition.spec.output_schema)
    nodes = tuple(
        _compile_node(node, context)
        for node in sorted(definition.spec.nodes, key=lambda candidate: candidate.id)
    )
    transitions = tuple(
        TransitionIr(
            id=transition.id,
            source=transition.source,
            target=transition.target,
            on=transition.on,
            when=transition.when,
            priority=transition.priority,
        )
        for transition in sorted(
            definition.spec.transitions,
            key=lambda item: (item.source, item.on, item.priority, item.target, item.id),
        )
    )
    flow = definition.spec.flow
    initial_node_id = flow.initial_node if isinstance(flow, StateMachineFlow) else None
    max_activations = flow.max_total_activations if isinstance(flow, StateMachineFlow) else None
    continue_every = flow.continue_as_new_every if isinstance(flow, StateMachineFlow) else None
    output_bindings = tuple(
        BindingIr(name=binding.name, expression=binding.expression)
        for binding in sorted(definition.spec.outputs, key=lambda item: item.name)
    )
    unhashed = ExecutablePlan(
        workflow_id=definition.metadata.id,
        workflow_version=definition.metadata.version,
        mode=flow.type,
        failure_policy=definition.spec.failure_policy,
        max_concurrency=definition.spec.max_concurrency,
        initial_node_id=initial_node_id,
        max_total_activations=max_activations,
        continue_as_new_every=continue_every,
        input_schema=input_schema,
        output_schema=output_schema,
        nodes=nodes,
        transitions=transitions,
        output_bindings=output_bindings,
        catalog_revision=context.catalog_revision,
        compiler_version=COMPILER_VERSION,
        plan_hash="0" * 64,
    )
    hash_payload = unhashed.model_dump(mode="json", exclude={"plan_hash"})
    return unhashed.model_copy(update={"plan_hash": sha256_text(canonical_json(hash_payload))})


def _collect_issues(
    definition: WorkflowDefinition,
    context: CompilationContext,
) -> list[CompilationIssue]:
    problems: list[CompilationIssue] = []
    problems.extend(
        validate_strict_schema(
            definition.spec.input_schema,
            path="/spec/inputSchema",
            complete_required=False,
        )
    )
    if len(definition.spec.nodes) > MAX_WORKFLOW_NODES:
        problems.append(
            issue(
                "workflow.node_limit_exceeded",
                "/spec/nodes",
                f"workflow cannot contain more than {MAX_WORKFLOW_NODES} nodes",
                limit=MAX_WORKFLOW_NODES,
                actual=len(definition.spec.nodes),
            )
        )
    if len(definition.spec.transitions) > MAX_WORKFLOW_TRANSITIONS:
        problems.append(
            issue(
                "workflow.transition_limit_exceeded",
                "/spec/transitions",
                f"workflow cannot contain more than {MAX_WORKFLOW_TRANSITIONS} transitions",
                limit=MAX_WORKFLOW_TRANSITIONS,
                actual=len(definition.spec.transitions),
            )
        )
    problems.extend(
        validate_strict_schema(
            definition.spec.output_schema,
            path="/spec/outputSchema",
            complete_required=True,
        )
    )

    node_ids: set[str] = set()
    for index, node in enumerate(definition.spec.nodes):
        node_path = f"/spec/nodes/{index}"
        if node.id in node_ids:
            problems.append(issue("node.duplicate_id", f"{node_path}/id", "node ID is duplicated"))
        node_ids.add(node.id)
        problems.extend(_node_issues(node, node_path, context))

    transition_ids: set[str] = set()
    logical_edges: set[tuple[str, str, str, int]] = set()
    priorities: set[tuple[str, str, int]] = set()
    for index, transition in enumerate(definition.spec.transitions):
        path = f"/spec/transitions/{index}"
        if transition.id in transition_ids:
            problems.append(issue("edge.duplicate_id", f"{path}/id", "transition ID is duplicated"))
        transition_ids.add(transition.id)
        if transition.source not in node_ids:
            problems.append(
                issue("edge.unknown_source", f"{path}/from", "transition source does not exist")
            )
        if transition.target not in node_ids:
            problems.append(
                issue("edge.unknown_target", f"{path}/to", "transition target does not exist")
            )
        if definition.spec.flow.type == "dag" and transition.source == transition.target:
            problems.append(issue("edge.self_loop", path, "self-loop transitions are forbidden"))
        logical = (transition.source, transition.target, transition.on, transition.priority)
        if logical in logical_edges:
            problems.append(issue("edge.duplicate", path, "logical transition is duplicated"))
        logical_edges.add(logical)
        priority = (transition.source, transition.on, transition.priority)
        if priority in priorities:
            problems.append(
                issue(
                    "edge.duplicate_priority",
                    f"{path}/priority",
                    "priority must be unique per source and outcome",
                )
            )
        priorities.add(priority)
        if transition.when is not None:
            expression_issue = validate_expression(transition.when, path=f"{path}/when")
            if expression_issue is not None:
                problems.append(expression_issue)

    output_names: set[str] = set()
    for index, binding in enumerate(definition.spec.outputs):
        if binding.name in output_names:
            problems.append(
                issue(
                    "binding.duplicate_name",
                    f"/spec/outputs/{index}/name",
                    "output binding name is duplicated",
                )
            )
        output_names.add(binding.name)
        expression_issue = validate_expression(
            binding.expression,
            path=f"/spec/outputs/{index}/expression",
        )
        if expression_issue is not None:
            problems.append(expression_issue)

    graph_limits_exceeded = any(
        problem.code in {"workflow.node_limit_exceeded", "workflow.transition_limit_exceeded"}
        for problem in problems
    )
    if not graph_limits_exceeded and not any(
        problem.code.startswith("edge.unknown_") for problem in problems
    ):
        problems.extend(_graph_issues(definition))
    return problems


def _node_issues(
    node: NodeDefinition,
    path: str,
    context: CompilationContext,
) -> list[CompilationIssue]:
    problems: list[CompilationIssue] = []
    input_names: set[str] = set()
    for index, binding in enumerate(node.inputs):
        if binding.name in input_names:
            problems.append(
                issue(
                    "binding.duplicate_name",
                    f"{path}/inputs/{index}/name",
                    "input binding name is duplicated",
                )
            )
        input_names.add(binding.name)
        expression_issue = validate_expression(
            binding.expression,
            path=f"{path}/inputs/{index}/expression",
        )
        if expression_issue is not None:
            problems.append(expression_issue)

    if isinstance(node, AgentNode):
        problems.extend(
            validate_strict_schema(
                node.output_schema,
                path=f"{path}/outputSchema",
                complete_required=True,
            )
        )
        supported = next(
            (
                model
                for model in context.provider_models
                if model.provider == node.agent.provider and model.model == node.agent.model
            ),
            None,
        )
        if supported is None:
            problems.append(
                issue(
                    "agent.model_unsupported",
                    f"{path}/agent/model",
                    "provider/model is not in the compilation catalog",
                )
            )
        elif node.agent.effort not in supported.efforts:
            problems.append(
                issue(
                    "agent.effort_unsupported",
                    f"{path}/agent/effort",
                    "effort is not supported by the selected model",
                )
            )
        if node.agent.workspace_id not in context.workspace_ids:
            problems.append(
                issue(
                    "workspace.unknown",
                    f"{path}/agent/workspaceId",
                    "workspace is not registered",
                )
            )
        if node.agent.provider_session_expression is not None:
            expression_issue = validate_expression(
                node.agent.provider_session_expression,
                path=f"{path}/agent/providerSessionExpression",
            )
            if expression_issue is not None:
                problems.append(expression_issue)
        if node.agent.access == "workspace_write" and node.agent.retry.maximum_attempts != 1:
            problems.append(
                issue(
                    "agent.write_retry_forbidden",
                    f"{path}/agent/retry/maximumAttempts",
                    "workspace-write agents must use exactly one automatic attempt",
                )
            )
    elif isinstance(node, ActivityNode):
        registered = next(
            (
                activity
                for activity in context.activities
                if activity.name == node.activity.name and activity.version == node.activity.version
            ),
            None,
        )
        if registered is None:
            problems.append(
                issue(
                    "activity.unregistered",
                    f"{path}/activity",
                    "activity name/version is not registered",
                )
            )
        else:
            problems.extend(
                validate_strict_schema(
                    registered.output_schema,
                    path=f"{path}/activity/outputSchema",
                    complete_required=True,
                )
            )
    elif isinstance(node, DecisionNode):
        expression_issue = validate_expression(node.expression, path=f"{path}/expression")
        if expression_issue is not None:
            problems.append(expression_issue)
    elif isinstance(node, EventWaitNode):
        problems.extend(
            validate_strict_schema(
                node.output_schema,
                path=f"{path}/outputSchema",
                complete_required=True,
            )
        )
        if node.correlation_expression is not None:
            expression_issue = validate_expression(
                node.correlation_expression,
                path=f"{path}/correlationExpression",
            )
            if expression_issue is not None:
                problems.append(expression_issue)
    return problems


def _graph_issues(definition: WorkflowDefinition) -> list[CompilationIssue]:
    graph: nx.DiGraph[str, dict[str, object], dict[str, object]] = nx.DiGraph()
    node_ids = {node.id for node in definition.spec.nodes}
    graph.add_nodes_from(node.id for node in definition.spec.nodes)
    graph.add_edges_from(
        (transition.source, transition.target) for transition in definition.spec.transitions
    )
    flow = definition.spec.flow
    if flow.type == "dag":
        problems = _join_issues(definition)
        if not nx.is_directed_acyclic_graph(graph):
            problems.append(issue("dag.cycle", "/spec/transitions", "DAG contains a cycle"))
        return problems

    assert isinstance(flow, StateMachineFlow)
    problems: list[CompilationIssue] = []
    if flow.initial_node not in node_ids:
        problems.append(
            issue(
                "state.initial_missing",
                "/spec/flow/initialNode",
                "initial node does not exist",
            )
        )
        return problems
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for transition in definition.spec.transitions:
        adjacency[transition.source].append(transition.target)
    reachable = {flow.initial_node}
    pending = [flow.initial_node]
    while pending:
        source = pending.pop()
        for target in adjacency[source]:
            if target not in reachable:
                reachable.add(target)
                pending.append(target)
    unreachable = sorted(node_ids - reachable)
    for node_id in unreachable:
        problems.append(
            issue(
                "state.unreachable_node",
                "/spec/nodes",
                "state-machine node is unreachable",
                node_id=node_id,
            )
        )
    if any(isinstance(node, JoinNode) for node in definition.spec.nodes):
        problems.append(
            issue(
                "state.join_forbidden",
                "/spec/nodes",
                "join nodes are only valid in DAG workflows",
            )
        )
    problems.extend(_state_transition_issues(definition))
    return problems


def _join_issues(definition: WorkflowDefinition) -> list[CompilationIssue]:
    incoming_sources: dict[str, set[str]] = {}
    for transition in definition.spec.transitions:
        incoming_sources.setdefault(transition.target, set()).add(transition.source)

    problems: list[CompilationIssue] = []
    for index, node in enumerate(definition.spec.nodes):
        if not isinstance(node, JoinNode) or node.mode != "quorum":
            continue
        assert node.required is not None
        incoming_count = len(incoming_sources.get(node.id, set()))
        if node.required > incoming_count:
            problems.append(
                issue(
                    "join.quorum_exceeds_incoming",
                    f"/spec/nodes/{index}/required",
                    "quorum cannot exceed the number of distinct incoming nodes",
                    required=node.required,
                    incoming=incoming_count,
                )
            )
    return problems


def _state_transition_issues(definition: WorkflowDefinition) -> list[CompilationIssue]:
    grouped: dict[tuple[str, str], list[tuple[int, TransitionDefinition]]] = {}
    for index, transition in enumerate(definition.spec.transitions):
        grouped.setdefault((transition.source, transition.on), []).append((index, transition))

    problems: list[CompilationIssue] = []
    for transitions in grouped.values():
        unconditional = [item for item in transitions if item[1].when is None]
        if len(unconditional) > 1:
            for index, _transition in unconditional[1:]:
                problems.append(
                    issue(
                        "state.multiple_fallbacks",
                        f"/spec/transitions/{index}/when",
                        "a state outcome can have at most one unconditional fallback",
                    )
                )
        if not unconditional:
            continue
        fallback_index, fallback = unconditional[0]
        conditional_priorities = [
            transition.priority for _index, transition in transitions if transition.when is not None
        ]
        if conditional_priorities and fallback.priority <= max(conditional_priorities):
            problems.append(
                issue(
                    "state.fallback_not_last",
                    f"/spec/transitions/{fallback_index}/priority",
                    "unconditional fallback must have lower precedence than every condition",
                )
            )
    return problems


def _compile_node(node: NodeDefinition, context: CompilationContext) -> NodeIr:
    output_schema = _node_output_schema(node, context)
    inputs = tuple(
        BindingIr(name=binding.name, expression=binding.expression)
        for binding in sorted(node.inputs, key=lambda item: item.name)
    )
    if isinstance(node, AgentNode):
        execution = AgentExecutionIr(
            provider=node.agent.provider,
            model=node.agent.model,
            effort=node.agent.effort,
            workspace_id=node.agent.workspace_id,
            access=node.agent.access,
            approval_mode=node.agent.approval_mode,
            network_policy=node.agent.network_policy,
            allowed_tool_profile=node.agent.allowed_tool_profile,
            session_mode=node.agent.session_mode,
            provider_session_expression=node.agent.provider_session_expression,
            instruction=node.agent.instruction,
            timeout_ms=_milliseconds(node.agent.timeout),
            retry=_retry_ir(node.agent.retry),
        )
    elif isinstance(node, ActivityNode):
        execution = ActivityExecutionIr(
            name=node.activity.name,
            version=node.activity.version,
            timeout_ms=_milliseconds(node.activity.timeout),
            retry=_retry_ir(node.activity.retry),
        )
    elif isinstance(node, DecisionNode):
        execution = DecisionExecutionIr(expression=node.expression)
    elif isinstance(node, ApprovalNode):
        execution = ApprovalExecutionIr(label=node.label, timeout_ms=_milliseconds(node.timeout))
    elif isinstance(node, EventWaitNode):
        execution = EventWaitExecutionIr(
            event_type=node.event_type,
            source_pattern=node.source_pattern,
            subject_pattern=node.subject_pattern,
            correlation_expression=node.correlation_expression,
            timeout_ms=_milliseconds(node.timeout),
        )
    elif isinstance(node, TimerNode):
        execution = TimerExecutionIr(delay_ms=_milliseconds(node.delay))
    else:
        execution = JoinExecutionIr(mode=node.mode, required=node.required)
    return NodeIr(
        id=node.id,
        type_version=node.type_version,
        inputs=inputs,
        output_schema=_schema_ir(output_schema),
        execution=execution,
    )


def _node_output_schema(node: NodeDefinition, context: CompilationContext) -> JsonObject:
    if isinstance(node, AgentNode):
        return node.output_schema
    if isinstance(node, ActivityNode):
        return next(
            activity.output_schema
            for activity in context.activities
            if activity.name == node.activity.name and activity.version == node.activity.version
        )
    if isinstance(node, DecisionNode):
        return _DECISION_SCHEMA
    if isinstance(node, ApprovalNode):
        return _APPROVAL_SCHEMA
    if isinstance(node, EventWaitNode):
        return node.output_schema
    if isinstance(node, TimerNode):
        return _TIMER_SCHEMA
    return _JOIN_SCHEMA


def _schema_ir(schema: JsonObject) -> StrictSchemaIr:
    canonical = canonical_json(schema)
    return StrictSchemaIr(canonical=canonical, sha256=sha256_text(canonical))


def _retry_ir(retry: RetryDefinition) -> RetryIr:
    return RetryIr(
        maximum_attempts=retry.maximum_attempts,
        initial_interval_ms=_milliseconds(retry.initial_interval),
        maximum_interval_ms=_milliseconds(retry.maximum_interval),
    )


def _milliseconds(value: timedelta) -> int:
    return int(value.total_seconds() * 1000)
