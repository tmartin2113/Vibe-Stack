"""
Paperclip Heartbeat Execution Mode

Implements the Paperclip heartbeat procedure for Vibe agents:

1. Read PAPERCLIP_* env vars (injected by the Paperclip adapter)
2. Fetch assigned tasks from Paperclip API
3. Checkout a task (atomic, conflict-safe)
4. Build context from issue + ancestors + comments
5. Detect if this is a clarification resume (human replied to agent question)
6. Run Vibe workflow (router → skills → specialist → critic)
7. Post results back to Paperclip (status update + comment + clarification)
8. Report cost events
9. Exit with structured JSON output for the adapter to parse

Human-in-the-loop flow:
    Agent blocks with clarification questions → Paperclip surfaces to human →
    Human replies as comment → Paperclip wakes agent with WAKE_COMMENT_ID →
    Agent detects resume, injects human reply into workflow context → continues.

Usage:
    python -m agents.main --heartbeat
"""

import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .cancellation import (
    CancellationToken,
    WorkflowCancelledError,
    start_cancellation_poller,
)
from .config import SystemConfig
# TYPE_CHECKING would create a circular import; SpendingTracker is lazily
# imported at runtime in _get_spending_tracker() — we just use string
# annotations for type hints.
from .heartbeat_progress import (
    PROGRESS_NODES as _PROGRESS_NODES,
    make_progress_callback as _make_progress_callback,
)
from .heartbeat_signals import (
    SigtermReceived as _SigtermReceived,
    install_sigterm_handler as _install_sigterm_handler,
    post_sigterm_partial as _post_sigterm_partial,
    restore_sigterm_handler as _restore_sigterm_handler,
)
from .main import set_paperclip_context, clear_request_context
from .metrics import metrics
from .paperclip_client import (
    CheckoutResult,
    Issue,
    PaperclipAPIError,
    PaperclipClient,
    PaperclipConflictError,
)
from .workflow_factory import WorkflowFactory

logger = logging.getLogger(__name__)


@dataclass
class ClarificationRequest:
    """Structured clarification request for the orchestrator to surface to a human."""
    questions: List[str]
    blocking_node: str = ""
    context_summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "questions": self.questions,
            "blocking_node": self.blocking_node,
            "context_summary": self.context_summary,
        }


@dataclass
class HeartbeatResult:
    """Result of a single heartbeat execution, serialised to stdout."""
    status: str  # "success", "idle", "blocked", "clarification_needed", "cancelled", "failed", "circuit_breaker"
    issue_id: str = ""
    summary: str = ""
    usage: Dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    cost_cents: int = 0
    provider: str = ""
    model: str = ""
    exit_code: int = 0
    clarification: Optional[Dict[str, Any]] = None
    retry_after_seconds: Optional[int] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _try_connect_ws(client: PaperclipClient):
    """Best-effort WebSocket connection. Returns PaperclipWSClient or None."""
    try:
        from .ws_client import PaperclipWSClient
    except ImportError:
        logger.debug("websockets not available — skipping WS client")
        return None

    try:
        ws = PaperclipWSClient(
            api_url=client.api_url,
            company_id=client.company_id,
            api_key=client.api_key,
        )
        ws.start()
        if ws.wait_connected(timeout=3.0):
            logger.info("WebSocket client connected")
            return ws
        else:
            logger.debug("WebSocket connection timed out — continuing without WS")
            ws.stop()
            return None
    except Exception as e:
        logger.debug("WebSocket client init failed (non-fatal): %s", e)
        return None


# Default readiness probe settings
_READINESS_MAX_WAIT = 120  # seconds — total time to wait for server
_READINESS_INITIAL_DELAY = 1.0  # seconds — first retry delay
_READINESS_MAX_DELAY = 15.0  # seconds — cap on backoff delay


def _wait_for_server(
    config: SystemConfig,
    max_wait: float = _READINESS_MAX_WAIT,
    initial_delay: float = _READINESS_INITIAL_DELAY,
    max_delay: float = _READINESS_MAX_DELAY,
) -> bool:
    """
    Block until the Paperclip API is reachable, with exponential backoff.

    Returns True if the server became healthy within *max_wait* seconds,
    False if the deadline expired.  This prevents the heartbeat from
    dying immediately when the server hasn't started yet (e.g. during
    Docker Compose boot ordering races).
    """
    try:
        client = PaperclipClient(
            api_url=config.paperclip.api_url or None,
            api_key=config.paperclip.api_key or os.environ.get("PAPERCLIP_API_KEY", "placeholder"),
        )
    except ValueError:
        # Can't even build a client — missing API URL; let run_heartbeat
        # report the real validation error.
        return True

    deadline = time.monotonic() + max_wait
    delay = initial_delay
    attempt = 0

    while time.monotonic() < deadline:
        if client.health_check():
            if attempt > 0:
                logger.info(
                    "Paperclip server ready after %d probe(s)", attempt,
                )
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        sleep_time = min(delay, remaining)
        logger.info(
            "Paperclip server not ready (probe %d). Retrying in %.0fs...",
            attempt + 1, sleep_time,
        )
        time.sleep(sleep_time)
        delay = min(delay * 2, max_delay)
        attempt += 1

    logger.error(
        "Paperclip server not reachable after %.0fs — giving up", max_wait,
    )
    return False


def run_heartbeat(config: SystemConfig) -> HeartbeatResult:
    """
    Execute one Paperclip heartbeat cycle.

    Reads task assignments from Paperclip, runs the Vibe workflow
    on the highest-priority task, and posts results back.

    Returns:
        HeartbeatResult with status and metadata for the adapter.
    """
    heartbeat_start = time.monotonic()
    metrics.increment("vibe_heartbeat_total", labels={"status": "started"})

    def _finish(result: HeartbeatResult) -> HeartbeatResult:
        """Record final heartbeat metrics and return the result."""
        duration = time.monotonic() - heartbeat_start
        metrics.increment("vibe_heartbeat_total", labels={"status": result.status})
        metrics.observe("vibe_heartbeat_duration_seconds", duration, labels={"status": result.status})
        if result.usage:
            input_tokens = result.usage.get("input_tokens", 0)
            output_tokens = result.usage.get("output_tokens", 0)
            if input_tokens:
                metrics.increment("vibe_heartbeat_tokens_total", value=float(input_tokens), labels={"direction": "input"})
            if output_tokens:
                metrics.increment("vibe_heartbeat_tokens_total", value=float(output_tokens), labels={"direction": "output"})
        clear_request_context()
        return result

    # ── Step 0: Validate configuration ──
    config_issues = _validate_heartbeat_config(config)
    if config_issues:
        msg = "Configuration validation failed: " + "; ".join(config_issues)
        logger.error(msg)
        return _finish(HeartbeatResult(status="failed", summary=msg, exit_code=1))

    # ── Step 0b: Circuit breaker gate check ──
    tracker = _get_spending_tracker(config)
    if tracker is not None:
        breaker_status = tracker.check_circuit_breaker()
        if breaker_status is not None:
            logger.warning(
                "Circuit breaker OPEN: %s (retry after %ds)",
                breaker_status.reason, breaker_status.retry_after_seconds,
            )
            metrics.increment("vibe_heartbeat_total", labels={"status": "circuit_breaker"})
            return _finish(HeartbeatResult(
                status="circuit_breaker",
                summary=f"Circuit breaker open: {breaker_status.reason}",
                retry_after_seconds=breaker_status.retry_after_seconds,
                exit_code=0,
            ))

    # ── Step 0c: Auto-generate run ID for standalone heartbeat polling ──
    if not os.environ.get("PAPERCLIP_RUN_ID"):
        import uuid
        os.environ["PAPERCLIP_RUN_ID"] = str(uuid.uuid4())
        logger.info("Auto-generated PAPERCLIP_RUN_ID=%s", os.environ["PAPERCLIP_RUN_ID"])

    # ── Step 0d: Wait for Paperclip server to be reachable ──
    if not _wait_for_server(config):
        return _finish(HeartbeatResult(
            status="failed",
            summary="Paperclip server not reachable after readiness probe timeout",
            exit_code=0,  # exit 0 so Docker doesn't count this as a crash
        ))

    # ── Step 1: Connect to Paperclip ──
    try:
        client = _create_client(config)
    except ValueError as e:
        logger.error("Paperclip connection failed: %s", e)
        return _finish(HeartbeatResult(status="failed", summary=str(e), exit_code=1))

    # ── Step 2: Identity check ──
    try:
        identity = client.get_identity()
        logger.info("Heartbeat started for agent %s (%s)", identity.name, identity.id)
        # Set structured log context for all subsequent log entries
        set_paperclip_context(
            agent_id=identity.id,
            run_id=os.environ.get("PAPERCLIP_RUN_ID", ""),
            task_type=_resolve_task_type(config),
        )
    except PaperclipAPIError as e:
        logger.error("Identity check failed: %s", e)
        metrics.increment("vibe_paperclip_api_errors_total", labels={"endpoint": "identity"})
        return _finish(HeartbeatResult(status="failed", summary=str(e), exit_code=1))

    # ── Step 2b: Best-effort WebSocket connection ──
    ws_client = _try_connect_ws(client)

    # ── Step 3: Get assignments ──
    try:
        assignments = client.get_assignments()
    except PaperclipAPIError as e:
        logger.error("Failed to fetch assignments: %s", e)
        metrics.increment("vibe_paperclip_api_errors_total", labels={"endpoint": "assignments"})
        return _finish(HeartbeatResult(status="failed", summary=str(e), exit_code=1))

    if not assignments:
        logger.info("No tasks assigned — exiting idle")
        if tracker is not None:
            tracker.record_event(status="idle")
        return _finish(HeartbeatResult(status="idle", summary="No tasks assigned"))

    # ── Step 4: Pick work (with checkout fallthrough) ──
    candidates = _rank_tasks(assignments)
    if not candidates:
        logger.info("No actionable tasks — exiting idle")
        if tracker is not None:
            tracker.record_event(status="idle")
        return _finish(HeartbeatResult(status="idle", summary="No actionable tasks"))

    issue: Optional[Issue] = None
    checkout = None
    for candidate in candidates:
        logger.info("Trying task: %s (%s) [%s]", candidate.title, candidate.id, candidate.status)
        try:
            checkout = client.checkout_issue(candidate.id)
        except PaperclipAPIError as e:
            logger.warning("Checkout failed for %s — trying next: %s", candidate.id, e)
            metrics.increment("vibe_paperclip_api_errors_total", labels={"endpoint": "checkout"})
            continue

        if not checkout.success:
            logger.warning("Checkout conflict for %s — trying next", candidate.id)
            continue

        issue = candidate
        break

    if issue is None:
        logger.info("All candidates failed checkout — exiting idle")
        if tracker is not None:
            tracker.record_event(status="idle")
        return _finish(HeartbeatResult(status="idle", summary="No tasks available for checkout"))

    logger.info("Selected task: %s (%s) [%s]", issue.title, issue.id, issue.status)
    # Update log context with the selected issue
    set_paperclip_context(
        issue_id=issue.id,
        agent_id=identity.id,
        run_id=os.environ.get("PAPERCLIP_RUN_ID", ""),
        task_type=_resolve_task_type(config),
    )

    # ── Step 5b: Mark as in_progress so Paperclip UI reflects active work ──
    try:
        client.update_issue(issue.id, status="in_progress")
    except PaperclipAPIError as e:
        logger.warning("Failed to set in_progress for %s (non-fatal): %s", issue.id, e)

    # ── All remaining steps wrapped in try/finally to release checkout ──
    try:
        return _finish(_execute_checked_out_task(
            config, client, issue, tracker=tracker, ws_client=ws_client,
            identity=identity,
        ))
    finally:
        if ws_client is not None:
            ws_client.stop()

        # Best-effort message store maintenance
        try:
            if config.messages.cleanup_on_heartbeat or config.messages.backfill_on_heartbeat:
                from .message_store import get_shared_message_store
                msg_store = get_shared_message_store()
                if config.messages.cleanup_on_heartbeat:
                    expired = msg_store.cleanup_expired()
                    if expired:
                        logger.info("Heartbeat cleanup: removed %d expired messages", expired)
                if config.messages.backfill_on_heartbeat:
                    backfilled = msg_store.backfill_embeddings(
                        batch_size=config.messages.backfill_batch_size,
                    )
                    if backfilled:
                        logger.info("Heartbeat backfill: embedded %d messages", backfilled)
        except Exception as e:
            logger.debug("Message store maintenance skipped: %s", e)

        # Best-effort artifact cache maintenance
        _artifact_cache_maintenance()

        try:
            client.release_issue(issue.id)
        except PaperclipAPIError as e:
            logger.warning("Failed to release checkout for %s (non-fatal): %s", issue.id, e)


def _execute_checked_out_task(
    config: SystemConfig,
    client: PaperclipClient,
    issue: Issue,
    tracker: "Optional[SpendingTracker]" = None,
    ws_client=None,
    identity=None,
) -> HeartbeatResult:
    """Execute the workflow for a checked-out task. Caller must release checkout."""
    # ── Step 6: Build context ──
    try:
        ctx_start = time.monotonic()
        full_issue = client.get_issue(issue.id)
        comments = client.get_comments(issue.id)
        metrics.observe("vibe_paperclip_api_duration_seconds", time.monotonic() - ctx_start, labels={"endpoint": "context"})
        metrics.increment("vibe_paperclip_api_calls_total", labels={"endpoint": "get_issue"})
        metrics.increment("vibe_paperclip_api_calls_total", labels={"endpoint": "get_comments"})
    except PaperclipAPIError as e:
        logger.error("Failed to fetch issue context: %s", e)
        metrics.increment("vibe_paperclip_api_errors_total", labels={"endpoint": "context"})
        return HeartbeatResult(status="failed", issue_id=issue.id, summary=str(e), exit_code=1)

    task_type = _resolve_task_type(config)

    # ── Step 6b: Detect clarification resume ──
    # Use full_issue (fresh from API) not issue (stale from assignments)
    # to avoid race conditions where status changed between calls.
    clarification_reply = _detect_clarification_resume(
        full_issue, comments, client.agent_id,
    )
    if clarification_reply:
        logger.info(
            "Resuming blocked task %s with human clarification: %s",
            issue.id, clarification_reply[:80],
        )

    user_request = _build_user_request(full_issue, comments, clarification_reply)

    # ── Step 6c: Orchestrator detection ──
    if task_type == "orchestrator":
        from .orchestrator import run_orchestrator_heartbeat
        return run_orchestrator_heartbeat(
            config, client, full_issue,
            clarification_reply=clarification_reply,
            ws_client=ws_client,
        )

    # ── Step 7: Run workflow with cancellation monitoring ──
    # Extract complexity tier hint embedded by orchestrator parent
    complexity_tier = _extract_complexity_hint(full_issue.description)

    # Start cancellation watcher — WS push if available, otherwise HTTP poll
    cancel_token = CancellationToken()
    cancel_poller = start_cancellation_poller(
        client, issue.id, cancel_token, ws_client=ws_client,
    )

    # Progress callback — posts updates to Paperclip at key workflow nodes
    progress_cb = _make_progress_callback(client, issue.id)

    # SIGTERM handler — posts partial results on container shutdown
    sigterm_state: Dict[str, Any] = {}
    _install_sigterm_handler(client, issue.id, sigterm_state)

    workflow_start = time.monotonic()
    try:
        final_state = _run_workflow(
            config, user_request, task_type,
            complexity_tier=complexity_tier,
            cancellation_token=cancel_token,
            progress_callback=progress_cb,
            partial_state=sigterm_state,
            clarification_reply=clarification_reply,
        )
    except WorkflowCancelledError as e:
        logger.info("Workflow cancelled for %s: %s", issue.id, e.reason,
                     extra={"event": "workflow_cancelled", "issue_id": issue.id, "reason": e.reason})
        metrics.increment("vibe_heartbeat_total", labels={"status": "cancelled"})
        _post_cancelled(client, issue.id)
        return HeartbeatResult(
            status="cancelled",
            issue_id=issue.id,
            summary=f"Cancelled: {e.reason}",
            exit_code=0,
        )
    except _SigtermReceived:
        logger.info("SIGTERM received during workflow for %s", issue.id,
                     extra={"event": "sigterm", "issue_id": issue.id})
        metrics.increment("vibe_heartbeat_total", labels={"status": "sigterm"})
        _post_sigterm_partial(client, issue.id, sigterm_state)
        return HeartbeatResult(
            status="blocked",
            issue_id=issue.id,
            summary="Interrupted by SIGTERM — partial results posted",
            exit_code=0,
        )
    except Exception as e:
        logger.error("Workflow failed: %s", e, exc_info=True,
                      extra={"event": "workflow_error", "issue_id": issue.id, "error_type": type(e).__name__})
        metrics.increment("vibe_heartbeat_total", labels={"status": "workflow_error"})
        _post_failure(client, issue.id, str(e))
        return HeartbeatResult(
            status="failed",
            issue_id=issue.id,
            summary=f"Workflow error: {e}",
            exit_code=1,
        )
    finally:
        cancel_poller.stop()
        _restore_sigterm_handler()
    workflow_duration = time.monotonic() - workflow_start
    metrics.observe("vibe_heartbeat_workflow_duration_seconds", workflow_duration, labels={"task_type": task_type or "auto"})
    logger.info("Workflow completed in %.1fs", workflow_duration,
                 extra={"event": "workflow_complete", "issue_id": issue.id,
                        "duration_s": round(workflow_duration, 2), "task_type": task_type or "auto"})

    # ── Step 8: Check for clarification needs ──
    if final_state.get("clarification_needed"):
        questions = final_state.get("clarification_questions", [])
        if questions:
            clarification = ClarificationRequest(
                questions=questions,
                blocking_node=final_state.get("last_node", "specialist"),
                context_summary=final_state.get("specification", "")[:500],
            )
            comment_body = _format_clarification_comment(questions)
            try:
                api_start = time.monotonic()
                client.update_issue(
                    issue.id, status="blocked", comment=comment_body,
                )
                metrics.increment("vibe_paperclip_api_calls_total", labels={"endpoint": "update_issue"})
                metrics.observe("vibe_paperclip_api_duration_seconds", time.monotonic() - api_start, labels={"endpoint": "update_issue"})
            except PaperclipAPIError as e:
                logger.error("Failed to post clarification: %s", e)
                metrics.increment("vibe_paperclip_api_errors_total", labels={"endpoint": "update_issue"})

            usage = _extract_usage(final_state)
            return HeartbeatResult(
                status="clarification_needed",
                issue_id=issue.id,
                summary=f"Agent needs clarification ({len(questions)} questions)",
                usage=usage,
                cost_cents=0,
                provider=config.model.backend,
                model=config.model.model_name,
                exit_code=0,
                clarification=clarification.to_dict(),
            )

    # ── Step 9: Post results ──
    output = final_state.get("final_output", final_state.get("current_output", ""))
    score = final_state.get("final_score", final_state.get("critic_score", final_state.get("output_critic_score", 0)))
    quality_threshold = config.workflow.quality_threshold

    logger.info(
        "Result: output=%d chars, score=%s, decision=%s",
        len(output), score, final_state.get("quality_gate_decision", "?"),
    )

    if score >= quality_threshold:
        result_status = "success"
        issue_status = "done"
        comment_body = _format_success_comment(output, score)
    else:
        result_status = "blocked"
        issue_status = "blocked"
        comment_body = _format_blocked_comment(output, score, quality_threshold)

    try:
        api_start = time.monotonic()
        client.update_issue(issue.id, status=issue_status, comment=comment_body)
        metrics.increment("vibe_paperclip_api_calls_total", labels={"endpoint": "update_issue"})
        metrics.observe("vibe_paperclip_api_duration_seconds", time.monotonic() - api_start, labels={"endpoint": "update_issue"})
    except PaperclipAPIError as e:
        logger.error("Failed to post results: %s", e)
        metrics.increment("vibe_paperclip_api_errors_total", labels={"endpoint": "update_issue"})

    # ── Step 9b: Post self-upgrade status (if applicable) ──
    if final_state.get("upgrade_applied"):
        upgrade_comment = _format_upgrade_comment(
            description=final_state.get("upgrade_proposal_description", ""),
            branch=final_state.get("upgrade_branch", ""),
            commit=final_state.get("upgrade_commit", ""),
        )
        try:
            client.add_comment(issue.id, upgrade_comment)
            metrics.increment("vibe_paperclip_api_calls_total", labels={"endpoint": "add_comment"})
        except PaperclipAPIError as e:
            logger.warning("Failed to post self-upgrade comment (non-fatal): %s", e)
    elif final_state.get("upgrade_signals"):
        signal_count = len(final_state.get("upgrade_signals", []))
        logger.info(
            "Self-upgrade: %d signal(s) recorded (not yet enough to propose)",
            signal_count,
        )

    # ── Step 10: Report costs ──
    usage = _extract_usage(final_state)
    if config.paperclip.cost_reporting:
        try:
            api_start = time.monotonic()
            cost_cents = _estimate_cost_cents(
                config.model.backend,
                config.model.model_name,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
            )
            client.report_cost(
                provider=config.model.backend,
                model=config.model.model_name,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cost_cents=cost_cents,
                issue_id=issue.id,
            )
            metrics.increment("vibe_paperclip_api_calls_total", labels={"endpoint": "report_cost"})
            metrics.observe("vibe_paperclip_api_duration_seconds", time.monotonic() - api_start, labels={"endpoint": "report_cost"})
        except PaperclipAPIError as e:
            logger.warning("Cost reporting failed (non-fatal): %s", e)
            metrics.increment("vibe_paperclip_api_errors_total", labels={"endpoint": "report_cost"})

    cost_cents = _estimate_cost_cents(
        config.model.backend,
        config.model.model_name,
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
    )

    # ── Step 10b: Record spending event and evaluate circuit breaker ──
    if tracker is not None:
        output_tokens = usage.get("output_tokens", 0)
        gen_duration_ms = int(workflow_duration * 1000)
        tps = output_tokens / workflow_duration if workflow_duration > 0 and output_tokens > 0 else 0.0
        tracker.record_event(
            status=result_status,
            cost_cents=cost_cents,
            agent_id=os.environ.get("PAPERCLIP_AGENT_ID", ""),
            agent_name=identity.name if identity else "",
            run_id=os.environ.get("PAPERCLIP_RUN_ID", ""),
            issue_id=issue.id,
            provider=config.model.backend,
            model=config.model.model_name,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=output_tokens,
            tokens_per_second=round(tps, 2),
            generation_duration_ms=gen_duration_ms,
        )

    return HeartbeatResult(
        status=result_status,
        issue_id=issue.id,
        summary=output[:500] if output else "No output produced",
        usage=usage,
        cost_cents=cost_cents,
        provider=config.model.backend,
        model=config.model.model_name,
        exit_code=0 if result_status == "success" else 1,
    )


# ── Internal Helpers ──


# Per-million-token pricing in cents. Conservative estimates; actual pricing
# varies by tier and changes over time.  Local backends (vllm, ollama,
# llama.cpp) are free — they run on the agent's own hardware.
_PRICING_PER_MILLION: Dict[str, Dict[str, tuple]] = {
    # backend -> model_prefix -> (input_cents, output_cents) per 1M tokens
    "openai": {
        "gpt-4o": (250, 1000),
        "gpt-4o-mini": (15, 60),
        "gpt-4-turbo": (1000, 3000),
        "gpt-4": (3000, 6000),
        "gpt-3.5": (50, 150),
        "_default": (250, 1000),
    },
    "anthropic": {
        "claude-3-opus": (1500, 7500),
        "claude-3.5-sonnet": (300, 1500),
        "claude-3-sonnet": (300, 1500),
        "claude-3-haiku": (25, 125),
        "claude-3.5-haiku": (80, 400),
        "_default": (300, 1500),
    },
    "google": {
        "gemini-1.5-pro": (125, 500),
        "gemini-1.5-flash": (8, 30),
        "gemini-pro": (50, 150),
        "_default": (125, 500),
    },
}

# Local backends — always free
_FREE_BACKENDS = {"vllm", "ollama", "llama.cpp", "llamacpp"}


def _estimate_cost_cents(
    backend: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
) -> int:
    """
    Estimate cost in cents from backend, model, and token counts.

    Returns 0 for local backends (vLLM, Ollama, llama.cpp).
    For cloud backends, uses conservative per-million-token pricing.
    """
    backend_lower = backend.lower()
    if backend_lower in _FREE_BACKENDS or not input_tokens and not output_tokens:
        return 0

    pricing = _PRICING_PER_MILLION.get(backend_lower)
    if pricing is None:
        return 0

    # Find best matching model prefix (longest match wins to avoid
    # "gpt-4o" matching before "gpt-4o-mini")
    model_lower = model_name.lower()
    input_rate, output_rate = pricing["_default"]
    best_prefix_len = 0
    for prefix, rates in pricing.items():
        if prefix != "_default" and model_lower.startswith(prefix) and len(prefix) > best_prefix_len:
            input_rate, output_rate = rates
            best_prefix_len = len(prefix)

    cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    return max(1, round(cost))  # At least 1 cent if any cloud tokens used


def _create_client(config: SystemConfig) -> PaperclipClient:
    """Create PaperclipClient with config overrides applied.

    Raises ValueError if PAPERCLIP_AGENT_ID is not set, since
    self-comment filtering in clarification detection requires it.
    """
    kwargs: Dict[str, Any] = {}
    if config.paperclip.api_url:
        kwargs["api_url"] = config.paperclip.api_url
    if config.paperclip.api_key:
        kwargs["api_key"] = config.paperclip.api_key
    client = PaperclipClient(**kwargs)
    if not client.agent_id:
        raise ValueError(
            "PAPERCLIP_AGENT_ID not set — required for clarification self-comment filtering"
        )
    return client


def _rank_tasks(assignments: List[Issue]) -> List[Issue]:
    """
    Rank tasks by priority, returning a sorted list for fallthrough checkout.

    Priority order:
    1. PAPERCLIP_TASK_ID if set and in assignments (always first)
    2. in_progress tasks (resume existing work)
    3. todo tasks
    4. blocked only if explicitly woken for it
    """
    forced_task_id = os.environ.get("PAPERCLIP_TASK_ID", "").strip()
    ranked: List[Issue] = []
    seen_ids: set = set()

    # Forced task always first
    if forced_task_id:
        for issue in assignments:
            if issue.id == forced_task_id:
                ranked.append(issue)
                seen_ids.add(issue.id)
                break

    # in_progress first
    for issue in assignments:
        if issue.id not in seen_ids and issue.status == "in_progress":
            ranked.append(issue)
            seen_ids.add(issue.id)

    # then todo
    for issue in assignments:
        if issue.id not in seen_ids and issue.status == "todo":
            ranked.append(issue)
            seen_ids.add(issue.id)

    # blocked only if explicitly woken for it
    wake_reason = os.environ.get("PAPERCLIP_WAKE_REASON", "")
    if wake_reason in ("issue_comment_mentioned", "issue_assigned"):
        for issue in assignments:
            if issue.id not in seen_ids and issue.status == "blocked" and issue.id == forced_task_id:
                ranked.append(issue)
                seen_ids.add(issue.id)

    return ranked


def _resolve_task_type(config: SystemConfig) -> str:
    """Resolve the task type from config or env var."""
    if config.paperclip.task_type:
        return config.paperclip.task_type
    return os.environ.get("VIBE_TASK_TYPE", "")


def _build_user_request(
    issue: Issue,
    comments: list,
    clarification_reply: Optional[str] = None,
) -> str:
    """
    Build a user_request string from issue context.

    Includes title, description, ancestor chain (the "why"), and
    recent comments for additional context. When resuming from a
    clarification, the human's reply is injected prominently.
    """
    parts = []

    # Ancestor context (the "why" chain)
    if issue.ancestors:
        ancestor_chain = " → ".join(
            a.get("title", "Unknown") for a in reversed(issue.ancestors)
        )
        parts.append(f"Goal chain: {ancestor_chain}")

    # Primary task
    parts.append(f"Task: {issue.title}")
    if issue.description:
        parts.append(f"\n{issue.description}")

    # Human clarification reply — injected prominently before discussion
    if clarification_reply:
        parts.append(
            f"\n[Clarification from human]: {clarification_reply}"
        )

    # Recent comments for additional context (last 5)
    if comments:
        recent = comments[-5:]
        comment_text = "\n".join(f"- {c.body[:200]}" for c in recent)
        parts.append(f"\nRecent discussion:\n{comment_text}")

    return "\n".join(parts)


def _detect_clarification_resume(
    issue: Issue,
    comments: List[Any],
    agent_id: str,
) -> Optional[str]:
    """
    Detect if this heartbeat is a resume from a human clarification reply.

    Returns the human's reply text if all conditions are met:
    1. PAPERCLIP_WAKE_REASON is 'issue_comment_mentioned'
    2. PAPERCLIP_WAKE_COMMENT_ID is set
    3. The issue was previously blocked
    4. The wake comment is from a human (not this agent)

    Returns None if this is a normal (non-resume) invocation.
    """
    wake_reason = os.environ.get("PAPERCLIP_WAKE_REASON", "").strip()
    wake_comment_id = os.environ.get("PAPERCLIP_WAKE_COMMENT_ID", "").strip()

    if wake_reason != "issue_comment_mentioned" or not wake_comment_id:
        return None

    if issue.status != "blocked":
        return None

    # Find the specific wake comment
    for comment in comments:
        if comment.id == wake_comment_id:
            # Ensure it's from a human, not this agent echoing itself
            if comment.author_agent_id and comment.author_agent_id == agent_id:
                logger.debug("Wake comment %s is from self — not a clarification", wake_comment_id)
                return None
            # Strip whitespace; treat empty/whitespace-only replies as no reply
            body = (comment.body or "").strip()
            if not body:
                logger.debug("Wake comment %s has empty body — skipping", wake_comment_id)
                return None
            return body

    logger.warning("Wake comment %s not found in issue comments", wake_comment_id)
    return None


def _extract_complexity_hint(description: str) -> str:
    """Extract complexity tier from orchestrator-embedded HTML comment."""
    match = re.search(r"<!-- complexity:(\w+) -->", description or "")
    return match.group(1) if match else ""


def _run_workflow(
    config: SystemConfig,
    user_request: str,
    task_type: str,
    complexity_tier: str = "",
    cancellation_token: "Optional[CancellationToken]" = None,
    progress_callback: "Optional[Callable[[str, Dict[str, Any]], None]]" = None,
    partial_state: Optional[Dict[str, Any]] = None,
    clarification_reply: Optional[str] = None,
    factory: Optional[WorkflowFactory] = None,
) -> Dict[str, Any]:
    """
    Run the Vibe workflow graph on the given request.

    Delegates to ``WorkflowFactory`` for cached backend/adapter setup.
    Accepts an optional pre-built *factory* for reuse across invocations.
    """
    wf = factory or WorkflowFactory(config)
    return wf.run_workflow(
        user_request=user_request,
        task_type=task_type,
        complexity_tier=complexity_tier,
        cancellation_token=cancellation_token,
        progress_callback=progress_callback,
        partial_state=partial_state,
        clarification_reply=clarification_reply,
    )


def _extract_usage(state: Dict[str, Any]) -> Dict[str, int]:
    """Extract token usage from workflow state."""
    return {
        "input_tokens": state.get("total_input_tokens", 0),
        "output_tokens": state.get("total_output_tokens", 0),
    }


def _format_success_comment(output: str, score: int) -> str:
    """Format a success comment for Paperclip."""
    # Truncate output for comment (Paperclip comments should be concise)
    truncated = output[:3000] if output else "No output"
    return f"## Completed (score: {score}/100)\n\n{truncated}"


def _format_blocked_comment(output: str, score: int, threshold: int) -> str:
    """Format a blocked comment when quality gate fails."""
    truncated = output[:2000] if output else "No output"
    return (
        f"## Blocked — quality below threshold\n\n"
        f"Score: {score}/100 (threshold: {threshold})\n\n"
        f"### Output so far\n\n{truncated}\n\n"
        f"**Needs review or manual refinement.**"
    )


def _format_clarification_comment(questions: List[str]) -> str:
    """Format a clarification request comment for Paperclip."""
    lines = ["## Clarification Needed\n"]
    lines.append("I need some clarification before I can continue:\n")
    for i, q in enumerate(questions, 1):
        lines.append(f"{i}. {q}")
    lines.append("\n_Please reply to this comment with your answers._")
    return "\n".join(lines)


def _format_upgrade_comment(description: str, branch: str, commit: str) -> str:
    """Format a self-upgrade notification comment for Paperclip."""
    commit_short = commit[:8] if commit else "unknown"
    return (
        f"## Self-Upgrade Proposed\n\n"
        f"The agent identified an opportunity to improve its own code and "
        f"has committed a validated change for review.\n\n"
        f"**Improvement:** {description}\n"
        f"**Branch:** `{branch}`\n"
        f"**Commit:** `{commit_short}`\n\n"
        f"### Validation Gates Passed\n"
        f"- Path validation\n"
        f"- Diff size check\n"
        f"- Full pytest suite\n"
        f"- Bandit security scan\n\n"
        f"_Please review and merge the branch if the changes look good._"
    )


def _validate_heartbeat_config(config: SystemConfig) -> List[str]:
    """
    Validate config for heartbeat mode. Returns list of issues (empty = valid).

    Runs the base config.validate() plus heartbeat-specific checks.
    """
    issues: List[str] = []

    if not config.model.model_name:
        issues.append("Model name not specified")

    if config.mattermost.enabled and not config.mattermost.webhook_url:
        issues.append("Mattermost enabled but webhook URL not configured")

    # Heartbeat-specific: Paperclip connectivity requirements
    if not os.environ.get("PAPERCLIP_API_URL") and not config.paperclip.api_url:
        issues.append("PAPERCLIP_API_URL not set (required for heartbeat mode)")

    if not os.environ.get("PAPERCLIP_AGENT_ID"):
        issues.append("PAPERCLIP_AGENT_ID not set (required for self-comment filtering)")

    return issues


def _get_spending_tracker(config: SystemConfig) -> "Optional[SpendingTracker]":
    """Create a SpendingTracker if spending tracking is enabled."""
    if not config.spending.enabled:
        return None
    try:
        from .spending_tracker import SpendingTracker
        return SpendingTracker(
            db_path=config.spending.db_path,
            window_seconds=config.spending.window_seconds,
            max_cents_per_window=config.spending.max_cents_per_window,
            max_heartbeats_per_window=config.spending.max_heartbeats_per_window,
            max_consecutive_non_idle=config.spending.max_consecutive_non_idle,
            cooldown_seconds=config.spending.cooldown_seconds,
            max_cooldown_seconds=config.spending.max_cooldown_seconds,
            retention_days=config.spending.retention_days,
            agent_id=os.environ.get("PAPERCLIP_AGENT_ID", ""),
        )
    except Exception as e:
        logger.warning("Failed to initialize spending tracker (non-fatal): %s", e)
        return None


def _artifact_cache_maintenance() -> None:
    """Best-effort artifact cache cleanup: evict expired + LRU overflow."""
    try:
        from .artifact_store import ArtifactStore
        store = ArtifactStore()
        expired = store.cleanup_expired()
        if expired:
            logger.info("Heartbeat cache cleanup: removed %d expired artifacts", expired)
        # Also enforce LRU cap (separate from TTL expiry)
        conn = store._connect()
        try:
            evicted = store._evict_if_needed(conn)
            conn.commit()
            if evicted:
                logger.info("Heartbeat cache eviction: removed %d over-limit artifacts", evicted)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("Artifact cache maintenance skipped: %s", e)


def _post_failure(client: PaperclipClient, issue_id: str, error: str) -> None:
    """Post a failure comment and set issue to blocked."""
    try:
        client.update_issue(
            issue_id,
            status="blocked",
            comment=f"## Workflow Error\n\n```\n{error[:1000]}\n```\n\n**Agent encountered an error. Needs investigation.**",
        )
    except PaperclipAPIError as e:
        logger.error("Failed to post failure comment: %s", e)


def _post_cancelled(client: PaperclipClient, issue_id: str) -> None:
    """Post a cancellation comment. Issue is already 'cancelled' in Paperclip."""
    try:
        client.add_comment(
            issue_id,
            "## Cancelled\n\nWorkflow was cancelled before completion.",
        )
    except PaperclipAPIError as e:
        logger.warning("Failed to post cancellation comment (non-fatal): %s", e)
