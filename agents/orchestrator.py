"""
Orchestrator Bridge Agent — Fan-Out/Fan-In via Paperclip

Implements a 3-phase state machine for multi-agent task orchestration:

1. DECOMPOSE: Analyze parent issue → create child subtasks in Paperclip,
   each assigned to a specialized agent
2. POLL: Check child issue statuses. Auto-retry blocked children once.
   Block parent for human review if retries exhausted.
3. AGGREGATE: Collect completed child outputs → combine via AggregatorNode
   → post combined result on parent issue

The orchestrator is a regular Vibe agent with VIBE_TASK_TYPE=orchestrator.
Paperclip drives scheduling via heartbeats. All orchestration state is derived
from Paperclip issue state (no external storage needed).

Usage:
    Triggered from heartbeat.py when task_type == "orchestrator".
"""

import logging
import re
import threading
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .aggregator import AggregatorNode
from .config import SystemConfig
from .heartbeat import HeartbeatResult
from .paperclip_client import (
    AgentInfo,
    Comment,
    Issue,
    PaperclipAPIError,
    PaperclipClient,
)

logger = logging.getLogger(__name__)

# HTML comment markers used in issue comments to track orchestrator state
PLAN_MARKER = "<!-- orchestrator:plan -->"
STRATEGY_PATTERN = re.compile(r"<!-- strategy:(\w+) -->")
RETRY_MARKER_PATTERN = re.compile(r"<!-- retry:(\d+) -->")
RESULT_SCORE_PATTERN = re.compile(
    r"## Completed \(score: (\d+)/100\)\s*\n\n(.*)",
    re.DOTALL,
)


class OrchestratorPhase(Enum):
    """Orchestrator state machine phases, derived from issue state."""

    DECOMPOSE = "decompose"
    POLL = "poll"
    AGGREGATE = "aggregate"


def run_orchestrator_heartbeat(
    config: SystemConfig,
    client: PaperclipClient,
    issue: Issue,
    clarification_reply: Optional[str] = None,
    ws_client=None,
) -> HeartbeatResult:
    """
    Execute one orchestrator heartbeat cycle.

    Detects the current phase from Paperclip issue state and executes
    the appropriate handler: decompose, poll, or aggregate.

    Args:
        config: System configuration (includes orchestrator settings)
        client: Authenticated Paperclip API client
        issue: The parent issue assigned to the orchestrator
        clarification_reply: Human reply if resuming from clarification
        ws_client: Optional PaperclipWSClient for push-based POLL blocking

    Returns:
        HeartbeatResult for the adapter to parse
    """
    try:
        children = client.get_children(issue.id)
    except PaperclipAPIError as e:
        logger.error("Failed to fetch children for %s: %s", issue.id, e)
        return HeartbeatResult(
            status="failed",
            issue_id=issue.id,
            summary=f"Failed to fetch children: {e}",
            exit_code=1,
        )

    phase = _detect_phase(children)
    logger.info(
        "Orchestrator phase for %s: %s (%d children)",
        issue.id, phase.value, len(children),
    )

    if phase == OrchestratorPhase.DECOMPOSE:
        return _decompose_and_delegate(
            config, client, issue,
            clarification_reply=clarification_reply,
        )
    elif phase == OrchestratorPhase.AGGREGATE:
        return _aggregate_and_present(config, client, issue, children)
    else:
        return _poll_children(config, client, issue, children, ws_client=ws_client)


def _detect_phase(children: List[Issue]) -> OrchestratorPhase:
    """
    Determine orchestrator phase from child issue state.

    - No children → DECOMPOSE (first heartbeat)
    - All children done → AGGREGATE
    - Otherwise → POLL (waiting for workers)
    """
    if not children:
        return OrchestratorPhase.DECOMPOSE

    all_done = all(c.status == "done" for c in children)
    if all_done:
        return OrchestratorPhase.AGGREGATE

    return OrchestratorPhase.POLL


# ── Phase 1: DECOMPOSE ──


def _decompose_and_delegate(
    config: SystemConfig,
    client: PaperclipClient,
    issue: Issue,
    clarification_reply: Optional[str] = None,
) -> HeartbeatResult:
    """
    Analyze the parent issue and create child subtasks for specialist agents.

    Uses the RouterNode's decomposition logic to analyze the task, discovers
    available agents via Paperclip API, and creates child issues assigned
    to matching specialists.
    """
    from .complexity_triage import classify_complexity
    from .router import RouterNode
    from .state import create_initial_state

    user_request = f"{issue.title}\n\n{issue.description}" if issue.description else issue.title
    if clarification_reply:
        user_request += f"\n\n[Clarification from human]: {clarification_reply}"
    max_children = config.paperclip.orchestrator_max_children

    # Build a temporary state for the router to analyze
    state = create_initial_state(
        user_request=user_request,
        max_iterations=1,
        quality_threshold=config.workflow.quality_threshold,
    )
    state["specification"] = user_request

    # Triage complexity BEFORE decomposition (zero LLM calls)
    state = classify_complexity(state)
    tier = state["complexity_tier"]

    # Fast-tier tasks skip decomposition entirely
    if tier == "fast" and config.paperclip.orchestrator_skip_decomposition_for_fast:
        logger.info("Fast-tier task, skipping decomposition")
        return _run_directly(config, client, issue, user_request, complexity_tier="fast")

    # Use the router to classify and decompose
    router = RouterNode(classification_mode="regex")
    router_state = router.execute(state)

    if not router_state.get("requires_decomposition"):
        # Task doesn't need multi-agent — run directly as a normal workflow
        logger.info("Task does not require decomposition, running directly")
        return _run_directly(config, client, issue, user_request, complexity_tier=tier)

    sub_tasks = router_state.get("sub_tasks", [])
    aggregation_strategy = router_state.get("aggregation_strategy", "merge")

    if not sub_tasks:
        logger.warning("Decomposition returned empty sub_tasks")
        return _run_directly(config, client, issue, user_request)

    # Limit subtasks
    sub_tasks = sub_tasks[:max_children]

    # Dedup: check if parent already has children with matching titles
    try:
        existing_children = client.get_children(issue.id)
    except PaperclipAPIError:
        existing_children = []
    if existing_children:
        sub_tasks = _filter_duplicate_subtasks(sub_tasks, existing_children, issue.title)
        if not sub_tasks:
            logger.info("All proposed subtasks already exist as children — skipping decomposition")
            return HeartbeatResult(
                status="success",
                issue_id=issue.id,
                summary="All subtasks already exist (dedup)",
            )

    # Discover available agents
    try:
        agents = client.list_agents()
    except PaperclipAPIError as e:
        logger.error("Failed to list agents: %s", e)
        return HeartbeatResult(
            status="failed",
            issue_id=issue.id,
            summary=f"Agent discovery failed: {e}",
            exit_code=1,
        )

    agent_lookup = _build_agent_lookup(agents, client.agent_id)

    # Create child issues in Paperclip
    created_children = []
    for sub_task in sub_tasks:
        task_type = sub_task.get("task_type", "general")
        spec = sub_task.get("specification", "")
        title = f"[{task_type}] {issue.title}"

        assignee_id = _match_agent(task_type, agent_lookup)

        try:
            child = client.create_subtask(
                title=title[:200],
                description=f"<!-- complexity:{tier} -->\n{spec}",
                parent_id=issue.id,
                goal_id=issue.goal_id,
                assignee_agent_id=assignee_id,
            )
            created_children.append((child, task_type, assignee_id))
        except PaperclipAPIError as e:
            logger.error("Failed to create subtask for %s: %s", task_type, e)

    if not created_children:
        return HeartbeatResult(
            status="failed",
            issue_id=issue.id,
            summary="Failed to create any subtasks",
            exit_code=1,
        )

    if len(created_children) < len(sub_tasks):
        logger.warning(
            "Partial decomposition: created %d/%d subtasks for %s. "
            "Some task types will be missing from the final result.",
            len(created_children), len(sub_tasks), issue.id,
        )

    # Post plan comment on parent
    plan_lines = [
        f"{PLAN_MARKER}",
        f"<!-- strategy:{aggregation_strategy} -->",
        f"## Orchestrator Plan",
        f"",
        f"Decomposed into {len(created_children)} subtasks:",
        "",
    ]
    for child, task_type, assignee_id in created_children:
        agent_name = _find_agent_name(agents, assignee_id) if assignee_id else "unassigned"
        plan_lines.append(f"- **{task_type}** → {agent_name} ({child.id}) [tier: {tier}]")

    try:
        client.add_comment(issue.id, "\n".join(plan_lines))
    except PaperclipAPIError as e:
        logger.warning("Failed to post plan comment: %s", e)

    # Transition parent to in_progress so Paperclip UI reflects active orchestration
    try:
        client.update_issue(issue.id, status="in_progress")
    except PaperclipAPIError as e:
        logger.warning("Failed to set parent to in_progress: %s", e)

    logger.info(
        "Decomposed %s into %d subtasks (strategy: %s)",
        issue.id, len(created_children), aggregation_strategy,
    )

    return HeartbeatResult(
        status="success",
        issue_id=issue.id,
        summary=f"Decomposed into {len(created_children)} subtasks",
    )


def _run_directly(
    config: SystemConfig,
    client: PaperclipClient,
    issue: Issue,
    user_request: str,
    complexity_tier: str = "",
) -> HeartbeatResult:
    """Run a task directly when it doesn't need multi-agent decomposition."""
    from .heartbeat import (
        ClarificationRequest,
        _extract_usage,
        _format_blocked_comment,
        _format_clarification_comment,
        _format_success_comment,
        _run_workflow,
    )

    try:
        final_state = _run_workflow(config, user_request, "", complexity_tier=complexity_tier)
    except Exception as e:
        logger.error("Direct workflow failed: %s", e, exc_info=True)
        return HeartbeatResult(
            status="failed",
            issue_id=issue.id,
            summary=f"Workflow error: {e}",
            exit_code=1,
        )

    # Check for clarification needs (mirrors heartbeat.py step 8)
    if final_state.get("clarification_needed"):
        questions = final_state.get("clarification_questions", [])
        if questions:
            clarification = ClarificationRequest(
                questions=questions,
                blocking_node=final_state.get("last_node", "vibe"),
                context_summary=final_state.get("specification", "")[:500],
            )
            comment_body = _format_clarification_comment(questions)
            try:
                client.update_issue(issue.id, status="blocked", comment=comment_body)
            except PaperclipAPIError as e:
                logger.error("Failed to post clarification: %s", e)

            return HeartbeatResult(
                status="clarification_needed",
                issue_id=issue.id,
                summary=f"Agent needs clarification ({len(questions)} questions)",
                usage=_extract_usage(final_state),
                provider=config.model.backend,
                model=config.model.model_name,
                exit_code=0,
                clarification=clarification.to_dict(),
            )

    output = final_state.get("final_output", final_state.get("current_output", ""))
    score = final_state.get("final_score", final_state.get("critic_score", 0))
    quality_threshold = config.workflow.quality_threshold

    if score >= quality_threshold:
        comment = _format_success_comment(output, score)
        issue_status = "done"
        result_status = "success"
    else:
        comment = _format_blocked_comment(output, score, quality_threshold)
        issue_status = "blocked"
        result_status = "blocked"

    try:
        client.update_issue(issue.id, status=issue_status, comment=comment)
    except PaperclipAPIError as e:
        logger.error("Failed to post direct result: %s", e)

    # Report costs to Paperclip (mirrors heartbeat.py step 10)
    usage = _extract_usage(final_state)
    if config.paperclip.cost_reporting:
        try:
            client.report_cost(
                provider=config.model.backend,
                model=config.model.model_name,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cost_cents=0,  # Local models are free
                issue_id=issue.id,
            )
        except PaperclipAPIError as e:
            logger.warning("Cost reporting failed (non-fatal): %s", e)

    return HeartbeatResult(
        status=result_status,
        issue_id=issue.id,
        summary=output[:500] if output else "No output",
        usage=usage,
        provider=config.model.backend,
        model=config.model.model_name,
    )


# ── Phase 2: POLL ──


def _poll_children(
    config: SystemConfig,
    client: PaperclipClient,
    issue: Issue,
    children: List[Issue],
    ws_client=None,
) -> HeartbeatResult:
    """
    Check child issue statuses and handle retries.

    - Children still in_progress/todo → exit idle
    - Child blocked and not retried → auto-retry (reset to todo)
    - Child blocked and already retried → permanently failed
    - Any permanently failed → block parent for human review

    When *ws_client* is connected, blocks on WS events instead of
    exiting idle — avoids container respawn cycles.
    """
    poll_timeout = config.paperclip.orchestrator_poll_timeout

    return _poll_children_once(
        config, client, issue, children,
        ws_client=ws_client, poll_timeout=poll_timeout,
    )


def _poll_children_once(
    config: SystemConfig,
    client: PaperclipClient,
    issue: Issue,
    children: List[Issue],
    ws_client=None,
    poll_timeout: int = 300,
) -> HeartbeatResult:
    """Single evaluation of child statuses with optional WS-blocking loop."""
    max_retries = config.paperclip.orchestrator_max_retries
    retry_enabled = config.paperclip.orchestrator_retry_failed

    done_count = 0
    pending_count = 0
    permanently_failed: List[Issue] = []

    for child in children:
        if child.status == "done":
            done_count += 1
        elif child.status == "blocked":
            if retry_enabled and _maybe_retry_child(client, child, max_retries):
                pending_count += 1
            else:
                permanently_failed.append(child)
        else:
            # todo, in_progress, backlog
            pending_count += 1

    if permanently_failed:
        # If some children succeeded, aggregate partial results rather than
        # blocking everything. Only fully block if NO children succeeded.
        if done_count > 0:
            logger.info(
                "Partial failure: %d done, %d failed — aggregating available results",
                done_count, len(permanently_failed),
            )
            return _aggregate_with_partial_failures(
                config, client, issue, children, permanently_failed,
            )

        failed_names = ", ".join(c.title[:50] for c in permanently_failed)
        comment = (
            f"## Orchestrator: Blocked\n\n"
            f"{len(permanently_failed)} subtask(s) failed after retry:\n"
            f"- {failed_names}\n\n"
            f"**Needs human review.**"
        )
        try:
            client.update_issue(issue.id, status="blocked", comment=comment)
        except PaperclipAPIError as e:
            logger.error("Failed to block parent: %s", e)

        return HeartbeatResult(
            status="blocked",
            issue_id=issue.id,
            summary=f"{len(permanently_failed)} subtasks failed after retry",
            exit_code=1,
        )

    # Still waiting for children
    logger.info(
        "Orchestrator %s: %d/%d done, %d pending",
        issue.id, done_count, len(children), pending_count,
    )

    # ── WS-driven blocking: stay alive and wait for child status changes ──
    if ws_client is not None and ws_client.is_connected and pending_count > 0:
        result = _ws_wait_for_children(
            config, client, issue, children, ws_client, poll_timeout,
        )
        if result is not None:
            return result

    # Fallback: exit idle with retry hint (original behavior)
    return HeartbeatResult(
        status="idle",
        issue_id=issue.id,
        summary=f"Waiting: {done_count}/{len(children)} subtasks done",
        retry_after_seconds=30,
    )


def _ws_wait_for_children(
    config: SystemConfig,
    client: PaperclipClient,
    issue: Issue,
    children: List[Issue],
    ws_client,
    poll_timeout: int,
) -> Optional[HeartbeatResult]:
    """
    Subscribe to child status changes via WS and block until all done or timeout.

    Returns a HeartbeatResult if the phase resolves (AGGREGATE or permanent
    failure), or None to fall through to the idle exit.
    """
    import time as _time

    child_ids = {c.id for c in children if c.status != "done"}
    if not child_ids:
        return None

    wake_event = threading.Event()

    def _filter(event: dict) -> bool:
        payload = event.get("payload", {})
        return (
            event.get("type") == "issue.status_changed"
            and payload.get("issueId") in child_ids
        )

    def _handler(event: dict) -> None:
        wake_event.set()

    unsub = ws_client.subscribe(_filter, _handler)
    deadline = _time.monotonic() + poll_timeout

    try:
        while _time.monotonic() < deadline:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break

            logger.debug(
                "Orchestrator %s: WS-blocking for child updates (%.0fs remaining)",
                issue.id, remaining,
            )
            wake_event.wait(timeout=min(remaining, 60.0))
            wake_event.clear()

            # Re-fetch children to detect new phase
            try:
                children = client.get_children(issue.id)
            except PaperclipAPIError as e:
                logger.warning("Failed to refresh children: %s", e)
                continue

            phase = _detect_phase(children)
            if phase == OrchestratorPhase.AGGREGATE:
                logger.info("Orchestrator %s: all children done — proceeding to aggregate", issue.id)
                return _aggregate_and_present(config, client, issue, children)

            # Re-evaluate for retries / permanent failures
            pending = [c for c in children if c.status not in ("done",)]
            permanently_failed = []
            still_pending = 0
            for c in pending:
                if c.status == "blocked":
                    if not (config.paperclip.orchestrator_retry_failed
                            and _maybe_retry_child(client, c, config.paperclip.orchestrator_max_retries)):
                        permanently_failed.append(c)
                    else:
                        still_pending += 1
                else:
                    still_pending += 1

            if permanently_failed:
                done_count = sum(1 for c in children if c.status == "done")
                if done_count > 0:
                    return _aggregate_with_partial_failures(
                        config, client, issue, children, permanently_failed,
                    )
                failed_names = ", ".join(c.title[:50] for c in permanently_failed)
                comment = (
                    f"## Orchestrator: Blocked\n\n"
                    f"{len(permanently_failed)} subtask(s) failed after retry:\n"
                    f"- {failed_names}\n\n"
                    f"**Needs human review.**"
                )
                try:
                    client.update_issue(issue.id, status="blocked", comment=comment)
                except PaperclipAPIError:
                    pass
                return HeartbeatResult(
                    status="blocked",
                    issue_id=issue.id,
                    summary=f"{len(permanently_failed)} subtasks failed after retry",
                    exit_code=1,
                )

            # Update child_ids set for subscription filtering
            child_ids.clear()
            child_ids.update(c.id for c in children if c.status != "done")
            if not child_ids:
                # All done — one more phase check
                return _aggregate_and_present(config, client, issue, children)

        # Timeout — fall through to idle exit
        logger.info("Orchestrator %s: WS poll timeout (%ds) — exiting idle", issue.id, poll_timeout)
        return None
    finally:
        unsub()


def _maybe_retry_child(
    client: PaperclipClient,
    child: Issue,
    max_retries: int,
) -> bool:
    """
    Check if a blocked child should be retried, and retry it if so.

    Returns True if the child was retried or if we couldn't determine
    retry status (treat as pending — conservative-safe). Returns False
    only when retries are definitively exhausted.
    """
    try:
        comments = client.get_comments(child.id)
    except PaperclipAPIError as e:
        # Can't determine retry state — treat as pending (not permanently failed)
        logger.warning("Failed to fetch comments for %s, treating as pending: %s", child.id, e)
        return True

    retry_count = _count_retries(comments)
    if retry_count >= max_retries:
        return False

    # Perform retry: update status first, then mark with comment.
    # If the status update fails, the child stays blocked and we
    # return False so it's counted as permanently failed rather than
    # silently burning through retry attempts.
    next_retry = retry_count + 1
    try:
        client.update_issue(child.id, status="todo")
    except PaperclipAPIError as e:
        logger.error("Failed to reset child %s to todo: %s", child.id, e)
        return False

    try:
        client.add_comment(
            child.id,
            f"<!-- retry:{next_retry} --> Orchestrator auto-retry: resetting to todo",
        )
    except PaperclipAPIError as e:
        # Status was updated but comment failed — retry happened,
        # just the marker is missing. Log and continue.
        logger.warning("Retry marker comment failed for %s (retry still applied): %s", child.id, e)

    logger.info("Retried child %s (attempt %d)", child.id, next_retry)
    return True


def _count_retries(comments: List[Comment]) -> int:
    """Count retry markers in comments."""
    count = 0
    for comment in comments:
        match = RETRY_MARKER_PATTERN.search(comment.body)
        if match:
            count = max(count, int(match.group(1)))
    return count


# ── Phase 3: AGGREGATE ──


def _aggregate_and_present(
    config: SystemConfig,
    client: PaperclipClient,
    issue: Issue,
    children: List[Issue],
) -> HeartbeatResult:
    """
    Collect child outputs and aggregate into a combined result.

    Fetches result comments from each child issue, builds raw sections
    for the AggregatorNode, and posts the combined output on the parent.
    """
    # Idempotency: if parent is already done, skip aggregation to avoid
    # duplicate result comments on re-schedule (e.g., Paperclip timeout retry).
    if issue.status == "done":
        logger.info("Parent %s already done, skipping aggregation", issue.id)
        return HeartbeatResult(
            status="success",
            issue_id=issue.id,
            summary="Already aggregated (idempotent skip)",
        )

    # Determine aggregation strategy from the plan comment
    strategy = _extract_strategy(client, issue)

    # Collect child outputs
    raw_sections = []
    for child in children:
        task_type = _extract_task_type(child.title)
        output, score = _extract_child_result(client, child)

        if output:
            raw_sections.append({
                "task_type": task_type,
                "title": task_type.replace("_", " ").title(),
                "specialist": task_type,
                "output": output,
                "score": score,
                "specification": child.description or "",
            })

    if not raw_sections:
        comment = "## Orchestrator: No outputs to aggregate\n\nAll children completed but no result comments found."
        try:
            client.update_issue(issue.id, status="blocked", comment=comment)
        except PaperclipAPIError:
            pass
        return HeartbeatResult(
            status="blocked",
            issue_id=issue.id,
            summary="No child outputs to aggregate",
            exit_code=1,
        )

    # Build synthetic state for AggregatorNode
    user_request = f"{issue.title}\n\n{issue.description}" if issue.description else issue.title
    synthetic_state: Dict[str, Any] = {
        "sub_tasks": [
            {
                "task_type": s["task_type"],
                "specialist_adapter": s["specialist"],
                "specification": s["specification"],
                "output": s["output"],
                "output_score": s["score"],
                "status": "completed",
            }
            for s in raw_sections
        ],
        "aggregation_strategy": strategy,
        "user_request": user_request,
        "specification": user_request,
        "adapters_used": [],
    }

    # Try to create an adapter registry for LLM-driven aggregation
    adapter_registry = _create_aggregation_registry(config)
    aggregator = AggregatorNode(adapter_registry=adapter_registry)
    result_state = aggregator.execute(synthetic_state)

    aggregated_output = result_state.get("aggregated_output", "")
    avg_score = result_state.get("final_aggregation_score", 0)

    # Post combined result
    comment = (
        f"## Combined Result ({len(raw_sections)} specialists, avg score: {avg_score}/100)\n\n"
        f"{aggregated_output}"
    )

    try:
        client.update_issue(issue.id, status="done", comment=comment)
    except PaperclipAPIError as e:
        logger.error("Failed to post aggregated result: %s", e)
        return HeartbeatResult(
            status="failed",
            issue_id=issue.id,
            summary=f"Failed to post result: {e}",
            exit_code=1,
        )

    logger.info(
        "Aggregated %d child outputs for %s (avg score: %d)",
        len(raw_sections), issue.id, avg_score,
    )

    return HeartbeatResult(
        status="success",
        issue_id=issue.id,
        summary=aggregated_output[:500] if aggregated_output else "Aggregation complete",
    )


def _aggregate_with_partial_failures(
    config: SystemConfig,
    client: PaperclipClient,
    issue: Issue,
    children: List[Issue],
    permanently_failed: List[Issue],
) -> HeartbeatResult:
    """
    Aggregate results from succeeded children and note which ones failed.

    Unlike full aggregation, this produces a partial result with an explicit
    failures section, and marks the parent as 'done' (with caveats noted).
    """
    failed_ids = {c.id for c in permanently_failed}
    succeeded = [c for c in children if c.status == "done" and c.id not in failed_ids]

    # Collect outputs from succeeded children
    strategy = _extract_strategy(client, issue)
    raw_sections = []
    for child in succeeded:
        task_type = _extract_task_type(child.title)
        output, score = _extract_child_result(client, child)
        if output:
            raw_sections.append({
                "task_type": task_type,
                "title": task_type.replace("_", " ").title(),
                "specialist": task_type,
                "output": output,
                "score": score,
                "specification": child.description or "",
            })

    if not raw_sections:
        # All succeeded children had no extractable output — fall back to full block
        failed_names = ", ".join(c.title[:50] for c in permanently_failed)
        comment = (
            f"## Orchestrator: Blocked\n\n"
            f"{len(permanently_failed)} subtask(s) failed, "
            f"and {len(succeeded)} completed subtask(s) had no extractable output.\n"
            f"- Failed: {failed_names}\n\n"
            f"**Needs human review.**"
        )
        try:
            client.update_issue(issue.id, status="blocked", comment=comment)
        except PaperclipAPIError:
            pass
        return HeartbeatResult(
            status="blocked",
            issue_id=issue.id,
            summary="All outputs missing or failed",
            exit_code=1,
        )

    # Build synthetic state and aggregate
    user_request = f"{issue.title}\n\n{issue.description}" if issue.description else issue.title
    synthetic_state: Dict[str, Any] = {
        "sub_tasks": [
            {
                "task_type": s["task_type"],
                "specialist_adapter": s["specialist"],
                "specification": s["specification"],
                "output": s["output"],
                "output_score": s["score"],
                "status": "completed",
            }
            for s in raw_sections
        ],
        "aggregation_strategy": strategy,
        "user_request": user_request,
        "specification": user_request,
        "adapters_used": [],
    }

    adapter_registry = _create_aggregation_registry(config)
    aggregator = AggregatorNode(adapter_registry=adapter_registry)
    result_state = aggregator.execute(synthetic_state)

    aggregated_output = result_state.get("aggregated_output", "")
    avg_score = result_state.get("final_aggregation_score", 0)

    # Build failure notice
    failed_lines = []
    for child in permanently_failed:
        task_type = _extract_task_type(child.title)
        failed_lines.append(f"- **{task_type}**: {child.title[:80]} ({child.id})")

    comment = (
        f"## Partial Result ({len(raw_sections)}/{len(children)} specialists, "
        f"avg score: {avg_score}/100)\n\n"
        f"{aggregated_output}\n\n"
        f"---\n\n"
        f"### Failed Subtasks ({len(permanently_failed)})\n\n"
        f"The following subtasks failed after retry and are not included above:\n\n"
        + "\n".join(failed_lines)
        + "\n\n**Review the failed subtasks manually.**"
    )

    try:
        client.update_issue(issue.id, status="done", comment=comment)
    except PaperclipAPIError as e:
        logger.error("Failed to post partial result: %s", e)
        return HeartbeatResult(
            status="failed",
            issue_id=issue.id,
            summary=f"Failed to post result: {e}",
            exit_code=1,
        )

    logger.info(
        "Partial aggregation for %s: %d succeeded, %d failed",
        issue.id, len(raw_sections), len(permanently_failed),
    )

    return HeartbeatResult(
        status="success",
        issue_id=issue.id,
        summary=f"Partial: {len(raw_sections)}/{len(children)} specialists "
                f"({len(permanently_failed)} failed)",
    )


# ── Helpers ──


def _build_agent_lookup(
    agents: List[AgentInfo],
    self_agent_id: str,
) -> Dict[str, AgentInfo]:
    """
    Build a lookup mapping task-type keywords to agent info.

    Excludes the orchestrator itself. Matches agent roles to task types
    using keyword overlap (e.g., role "test_generator" matches "test_generation").
    """
    lookup: Dict[str, AgentInfo] = {}
    for agent in agents:
        if agent.id == self_agent_id:
            continue
        if agent.status != "active":
            continue

        # Use role as primary matching key
        role_lower = agent.role.lower()
        # Also check title for additional context
        title_lower = agent.title.lower() if agent.title else ""

        # Sort keywords longest-first so "review" matches before "code"
        # in roles like "code_reviewer", and "security" before "sec", etc.
        for keyword in sorted(_TASK_TYPE_KEYWORDS, key=len, reverse=True):
            if keyword in role_lower or keyword in title_lower:
                for task_type in _TASK_TYPE_KEYWORDS[keyword]:
                    if task_type not in lookup:
                        lookup[task_type] = agent

    return lookup


# Maps keywords found in agent roles/titles to task types
_TASK_TYPE_KEYWORDS: Dict[str, List[str]] = {
    "code": ["code_generation", "refactoring", "debugging"],
    "test": ["test_generation"],
    "security": ["security_audit"],
    "doc": ["documentation"],
    "research": ["research", "general"],
    "performance": ["performance_optimization"],
    "data": ["data_processing"],
    "api": ["api_development"],
    "database": ["database_operations"],
    "review": ["code_review"],
}


def _normalize_subtask_title(title: str) -> str:
    """Normalize a subtask title for dedup comparison.

    Lowercases and strips whitespace. Keeps the task-type prefix
    (e.g. '[code_generation]') so that different task types for the
    same parent are not treated as duplicates.
    """
    return title.lower().strip()


def _filter_duplicate_subtasks(
    proposed: List[Dict[str, Any]],
    existing_children: List[Issue],
    parent_title: str,
) -> List[Dict[str, Any]]:
    """Filter out proposed subtasks whose generated title would match an existing child."""
    existing_titles = set()
    for child in existing_children:
        existing_titles.add(_normalize_subtask_title(child.title))

    filtered = []
    for sub_task in proposed:
        task_type = sub_task.get("task_type", "general")
        would_be_title = f"[{task_type}] {parent_title}"
        normalized = _normalize_subtask_title(would_be_title)
        if normalized in existing_titles:
            logger.warning("Skipping duplicate subtask: %s (matches existing child)", would_be_title)
            continue
        filtered.append(sub_task)

    return filtered


def _match_agent(
    task_type: str,
    agent_lookup: Dict[str, AgentInfo],
) -> Optional[str]:
    """
    Find the best agent for a task type.

    Returns the agent_id if a match is found, None otherwise (Paperclip
    can assign the task later via its own routing).
    """
    agent = agent_lookup.get(task_type)
    if agent:
        return agent.id

    # Fallback: try "general" agent
    general_agent = agent_lookup.get("general")
    if general_agent:
        return general_agent.id

    return None


def _find_agent_name(agents: List[AgentInfo], agent_id: Optional[str]) -> str:
    """Find agent name by ID."""
    if not agent_id:
        return "unassigned"
    for agent in agents:
        if agent.id == agent_id:
            return agent.name or agent.role
    return agent_id[:8]


def _create_aggregation_registry(config: SystemConfig) -> Optional["AdapterRegistry"]:
    """
    Create a minimal adapter registry for LLM-driven aggregation.

    Returns an AdapterRegistry with a 'vibe' adapter if the LLM backend
    is available, or None to fall back to structured concatenation.
    """
    try:
        from .adapters import AdapterRegistry, PromptAdapter, VIBE_SYSTEM_PROMPT
        from .llm_backend import create_backend_from_config

        backend = create_backend_from_config(config)

        # Verify the backend is actually reachable before committing to LLM aggregation.
        # Without this, a non-functional backend would only fail at aggregation time,
        # silently falling back to concatenation after wasting time on the attempt.
        if not backend.health_check():
            logger.warning("LLM backend health check failed, using fallback concatenation")
            return None

        registry = AdapterRegistry()
        adapter = PromptAdapter(
            "vibe", VIBE_SYSTEM_PROMPT, backend,
            config=config.generation.get_config("vibe") if hasattr(config, "generation") else {},
        )
        registry.register(adapter)
        return registry
    except Exception as e:
        logger.warning(
            "Could not create LLM adapter for aggregation, using fallback concatenation: %s", e
        )
        return None


def _extract_strategy(client: PaperclipClient, issue: Issue) -> str:
    """Extract aggregation strategy from the orchestrator's plan comment."""
    try:
        comments = client.get_comments(issue.id)
    except PaperclipAPIError:
        return "merge"

    for comment in comments:
        match = STRATEGY_PATTERN.search(comment.body)
        if match:
            return match.group(1)

    return "merge"


def _extract_task_type(title: str) -> str:
    """Extract task_type from child issue title like '[test_generation] Build API'."""
    match = re.match(r"\[(\w+)\]", title)
    if match:
        return match.group(1)
    return "general"


def _extract_child_result(
    client: PaperclipClient,
    child: Issue,
) -> Tuple[str, int]:
    """
    Extract the result output and score from a child issue's comments.

    Looks for the standard format posted by heartbeat.py:
    '## Completed (score: 85/100)\n\n<output>'

    Returns:
        Tuple of (output_text, score). Empty string and 0 if not found.
    """
    try:
        comments = client.get_comments(child.id)
    except PaperclipAPIError:
        return "", 0

    # Search comments in reverse (most recent first)
    for comment in reversed(comments):
        match = RESULT_SCORE_PATTERN.search(comment.body)
        if match:
            score = int(match.group(1))
            output = match.group(2).strip()
            return output, score

    logger.warning(
        "Child %s (%s) is done but has no result comment matching expected format. "
        "Output will be missing from aggregation.",
        child.id, child.title,
    )
    return "", 0
