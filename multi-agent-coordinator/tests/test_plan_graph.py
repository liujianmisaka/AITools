from datetime import UTC, datetime, timedelta

import pytest

from misaka_coordinator_service.domain import (
    AgentSelection,
    CoordinatorDomainError,
    ExecutionReference,
    Plan,
    PlanDependency,
    PlanGraph,
    PlanNode,
    PlanNodeStatus,
    TaskIntent,
)

BASE_TIME = datetime(2026, 8, 27, 8, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return BASE_TIME + timedelta(minutes=minutes)


def make_plan() -> Plan:
    plan = Plan.draft(plan_id="plan-1", goal_id="goal-1", at=at(0))
    for index in range(1, 4):
        node = PlanNode.propose(
            node_id=f"node-{index}",
            intent=TaskIntent(task_id=f"task-{index}", objective=f"任务 {index}"),
            at=at(index),
        ).select(
            AgentSelection(
                provider_id="codex",
                model_id="pixel/gpt-5.6-luna",
                effort="medium",
                rationale="测试",
            ),
            at=at(index + 1),
        )
        plan = plan.add_node(node, at=at(index + 1))
    return plan


def accept_node(node: PlanNode, *, index: int) -> PlanNode:
    return (
        node.bind_execution(
            ExecutionReference(
                delegation_id=f"delegation-{index}",
                activation_id=f"activation-{index}",
                invocation_id=f"invocation-{index}",
                worker_session_id=f"worker-{index}",
            ),
            at=at(10 + index),
        )
        .request_review(at=at(20 + index))
        .accept(at=at(30 + index))
    )


def test_plan_graph_rejects_self_and_duplicate_dependencies() -> None:
    with pytest.raises(CoordinatorDomainError, match="itself"):
        PlanDependency(node_id="node-1", depends_on_node_id="node-1")

    graph = PlanGraph.empty(plan_id="plan-1", at=at(0)).add_dependency(
        node_id="node-2",
        depends_on_node_id="node-1",
        at=at(1),
    )
    with pytest.raises(CoordinatorDomainError, match="already exists"):
        graph.add_dependency(node_id="node-2", depends_on_node_id="node-1", at=at(2))


def test_plan_graph_validates_membership_and_cycles() -> None:
    plan = make_plan()
    unknown = PlanGraph.empty(plan_id="plan-1", at=at(0)).add_dependency(
        node_id="node-2",
        depends_on_node_id="missing",
        at=at(1),
    )
    with pytest.raises(CoordinatorDomainError, match="unknown dependency node"):
        unknown.validate(plan)

    with pytest.raises(CoordinatorDomainError, match="cycles"):
        PlanGraph.empty(plan_id="plan-1", at=at(0)).add_dependency(
            node_id="node-2",
            depends_on_node_id="node-1",
            at=at(1),
        ).add_dependency(
            node_id="node-1",
            depends_on_node_id="node-2",
            at=at(2),
        )


def test_plan_graph_orders_nodes_and_reports_ready_frontier() -> None:
    plan = make_plan()
    graph = (
        PlanGraph.empty(plan_id="plan-1", at=at(0))
        .add_dependency(node_id="node-2", depends_on_node_id="node-1", at=at(10))
        .add_dependency(node_id="node-3", depends_on_node_id="node-1", at=at(11))
        .add_dependency(node_id="node-3", depends_on_node_id="node-2", at=at(12))
    )

    assert graph.topological_order(plan) == ("node-1", "node-2", "node-3")
    assert graph.ready_node_ids(plan) == ("node-1",)

    accepted = accept_node(plan.nodes[0], index=1)
    plan = plan.replace_node(accepted, at=at(31))
    assert plan.nodes[0].status is PlanNodeStatus.ACCEPTED
    assert graph.ready_node_ids(plan) == ("node-2",)


def test_plan_graph_round_trips_and_removes_edges_monotonically() -> None:
    graph = PlanGraph.empty(plan_id="plan-1", at=at(0)).add_dependency(
        node_id="node-2",
        depends_on_node_id="node-1",
        at=at(1),
    )
    assert PlanGraph.from_dict(graph.to_dict()) == graph
    assert graph.dependencies_for("node-2") == ("node-1",)

    removed = graph.remove_dependency(
        node_id="node-2",
        depends_on_node_id="node-1",
        at=at(2),
    )
    assert removed.dependencies == ()
    assert removed.revision == graph.revision + 1
    with pytest.raises(CoordinatorDomainError, match="unknown plan dependency"):
        removed.remove_dependency(
            node_id="node-2",
            depends_on_node_id="node-1",
            at=at(3),
        )


def test_plan_graph_requires_matching_plan_identity() -> None:
    plan = make_plan()
    graph = PlanGraph.empty(plan_id="another-plan", at=at(0))
    with pytest.raises(CoordinatorDomainError, match="plan_id"):
        graph.validate(plan)
