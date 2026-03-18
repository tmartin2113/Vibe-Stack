"""
Main Entry Point for Multi-Agent System

This module initializes the system and provides CLI interface.

Usage:
    # Interactive mode
    python -m agents.main

    # Single request
    python -m agents.main "Write a Python script to analyze CSV files"

    # With options
    python -m agents.main "Your request" --max-iterations 5 --threshold 90
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.logging import RichHandler

from .config import SystemConfig, get_dev_config, get_production_config
from .adapters import (
    AdapterRegistry,
    PromptAdapter,
    GENESIA_SYSTEM_PROMPT,
    CRITIC_SYSTEM_PROMPT,
    REFINEMENT_SYSTEM_PROMPT,
    CODE_SYSTEM_PROMPT,
    CREATIVE_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
    DATA_SPECIALIST_PROMPT,
    API_GENERATOR_PROMPT,
    DATABASE_SPECIALIST_PROMPT,
    CODE_REVIEWER_PROMPT
)
from .graph import create_agent_graph, run_workflow, stream_workflow, print_graph_structure
from .state import create_initial_state
from .llm_backend import create_backend_from_config

console = Console()


import threading

# Thread-local storage for request context (request_id, session_id)
_request_context = threading.local()


def set_request_context(request_id: str = "", session_id: str = ""):
    """Set request tracing context for the current thread.

    Call this at the start of a worker handling a request so that all
    subsequent log entries on this thread include the IDs.
    """
    _request_context.request_id = request_id
    _request_context.session_id = session_id


def set_paperclip_context(
    issue_id: str = "",
    agent_id: str = "",
    run_id: str = "",
    task_type: str = "",
):
    """Set Paperclip-specific fields for structured logging.

    Call this at the start of a heartbeat run so all log entries
    on this thread include Paperclip tracing context.
    """
    _request_context.paperclip_issue_id = issue_id
    _request_context.paperclip_agent_id = agent_id
    _request_context.paperclip_run_id = run_id
    _request_context.paperclip_task_type = task_type


def clear_request_context():
    """Clear request tracing context for the current thread."""
    _request_context.request_id = ""
    _request_context.session_id = ""
    _request_context.paperclip_issue_id = ""
    _request_context.paperclip_agent_id = ""
    _request_context.paperclip_run_id = ""
    _request_context.paperclip_task_type = ""


class JsonFormatter(logging.Formatter):
    """
    Structured JSON log formatter for production/daemon mode.

    Produces one JSON object per line for easy ingestion by log
    aggregation systems (ELK, Loki, CloudWatch, etc.).

    Includes request_id and session_id when set via set_request_context(),
    and any extra fields passed via ``logger.info("msg", extra={...})``.

    Enable via LOG_FORMAT=json environment variable.
    """

    # Pre-compute the set of built-in LogRecord attributes so we can
    # detect caller-supplied extras without per-call overhead.
    _BUILTIN_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        # Include request tracing context if set
        request_id = getattr(_request_context, "request_id", "")
        session_id = getattr(_request_context, "session_id", "")
        if request_id:
            entry["request_id"] = request_id
        if session_id:
            entry["session_id"] = session_id
        # Paperclip tracing context
        for field in ("paperclip_issue_id", "paperclip_agent_id", "paperclip_run_id", "paperclip_task_type"):
            val = getattr(_request_context, field, "")
            if val:
                entry[field] = val
        # Include structured extra fields passed via logger.info("...", extra={"key": val})
        for key, val in record.__dict__.items():
            if key not in self._BUILTIN_ATTRS and key not in entry:
                entry[key] = val
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(config: SystemConfig) -> logging.Logger:
    """
    Configure logging for the application.

    Uses structured JSON output when LOG_FORMAT=json (recommended for
    production/daemon mode and log aggregation). Falls back to
    RichHandler for interactive/dev use.

    Args:
        config: System configuration (provides log level)

    Returns:
        Root 'agents' logger
    """
    log_format = os.environ.get("LOG_FORMAT", "").lower()

    handler: logging.Handler
    if log_format == "json":
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
    else:
        handler = RichHandler(rich_tracebacks=True, console=console)

    logging.basicConfig(
        level=config.log_level,
        format="%(message)s",
        handlers=[handler],
    )

    logger = logging.getLogger("agents")
    logger.setLevel(config.log_level)

    return logger


class MultiAgentSystem:
    """
    Main system class that initializes and manages the multi-agent workflow.
    """

    def __init__(self, config: Optional[SystemConfig] = None):
        self.config = config or get_dev_config()
        self.logger = setup_logging(self.config)

        self.base_model: Any = None
        self.adapter_registry: Optional[AdapterRegistry] = None
        self.graph: Any = None

    def initialize(self):
        """
        Initialize the system:
        1. Load base model
        2. Setup adapters (prompt-based)
        3. Create workflow
        """
        console.print("\n[bold blue]🚀 Initializing Multi-Agent System...[/bold blue]\n")

        # Validate configuration
        if not self.config.validate():
            console.print("[bold red]❌ Configuration validation failed[/bold red]")
            sys.exit(1)

        # Load base model
        console.print("📦 Loading base model...")
        self.base_model = self._load_base_model()
        console.print(f"   ✓ Loaded: {self.config.model.model_name}\n")

        # Setup adapters
        console.print("🔧 Setting up adapters...")
        self.adapter_registry = self._setup_adapters()
        assert self.adapter_registry is not None
        console.print(f"   ✓ Registered {len(self.adapter_registry.list_adapters())} adapters\n")

        # Create graph
        console.print("Building workflow...")
        self.graph = create_agent_graph(
            self.adapter_registry,
            config=self.config,
            base_model=self.base_model
        )
        console.print("   ✓ Workflow compiled\n")

        console.print("[bold green]✅ System initialized successfully![/bold green]\n")

    def _load_base_model(self):
        """
        Load the vLLM backend.

        Configure via environment variables:
        - GENESIA_MODEL: model name
        - GENESIA_BACKEND_HOST: server host (default: localhost)
        - GENESIA_BACKEND_PORT: server port (default: 8000)
        """
        try:
            backend = create_backend_from_config(self.config)
            return backend
        except Exception as e:
            self.logger.error(f"Failed to initialize LLM backend: {e}")
            self.logger.error(
                "Make sure vLLM is running:\n"
                "  python -m vllm.entrypoints.openai.api_server --model <model>"
            )
            raise

    def _setup_adapters(self) -> AdapterRegistry:
        """
        Setup adapter registry with all prompt-based adapters.

        Each adapter pairs a system prompt with per-task generation config.
        Task specialization beyond prompts is handled by the skills system.
        """
        registry = AdapterRegistry()

        # All adapters: (name, system_prompt)
        adapter_defs = [
            # Core workflow adapters
            ("genesia", GENESIA_SYSTEM_PROMPT),
            ("critic", CRITIC_SYSTEM_PROMPT),
            ("refinement", REFINEMENT_SYSTEM_PROMPT),
            # Task execution adapters
            ("code", CODE_SYSTEM_PROMPT),
            ("creative", CREATIVE_SYSTEM_PROMPT),
            ("research", RESEARCH_SYSTEM_PROMPT),
            ("general", RESEARCH_SYSTEM_PROMPT),
            # Specialist adapters
            ("data_specialist", DATA_SPECIALIST_PROMPT),
            ("api_generator", API_GENERATOR_PROMPT),
            ("database_specialist", DATABASE_SPECIALIST_PROMPT),
            ("code_reviewer", CODE_REVIEWER_PROMPT),
            ("test_generator", CODE_SYSTEM_PROMPT),
            ("security_auditor", CODE_SYSTEM_PROMPT),
            ("doc_generator", CODE_SYSTEM_PROMPT),
            ("performance_optimizer", CODE_SYSTEM_PROMPT),
            ("debugging_assistant", CODE_SYSTEM_PROMPT),
        ]

        for name, prompt in adapter_defs:
            adapter = PromptAdapter(name, prompt, self.base_model,
                                   config=self.config.generation.get_config(name))
            registry.register(adapter)

        return registry

    def run(
        self,
        user_request: str,
        max_iterations: Optional[int] = None,
        quality_threshold: Optional[int] = None,
        stream: bool = False
    ):
        """
        Run the workflow with a user request.

        Args:
            user_request: The user's input
            max_iterations: Override default max iterations
            quality_threshold: Override default quality threshold
            stream: Stream output in real-time

        Returns:
            Final state
        """
        if not self.graph:
            raise RuntimeError("System not initialized. Call initialize() first.")

        max_iter = max_iterations or self.config.workflow.max_iterations
        threshold = quality_threshold or self.config.workflow.quality_threshold

        if stream:
            return stream_workflow(self.graph, user_request, max_iter, threshold)
        else:
            return run_workflow(self.graph, user_request, max_iter, threshold, verbose=True)

    def interactive_mode(self):
        """
        Run in interactive mode - prompt user for requests.
        """
        console.print("\n[bold cyan]🤖 Interactive Multi-Agent Mode[/bold cyan]")
        console.print("Enter your requests (or 'quit' to exit)\n")

        while True:
            try:
                user_input = console.input("[bold green]You:[/bold green] ").strip()

                if not user_input:
                    continue

                if user_input.lower() in ['quit', 'exit', 'q']:
                    console.print("\n[bold]Goodbye! 👋[/bold]\n")
                    break

                # Handle commands
                if user_input.startswith('/'):
                    self._handle_command(user_input)
                    continue

                # Run workflow
                self.run(user_input, stream=True)

            except KeyboardInterrupt:
                console.print("\n\n[bold]Interrupted. Type 'quit' to exit.[/bold]\n")
            except Exception as e:
                console.print(f"\n[bold red]Error:[/bold red] {e}\n")
                self.logger.exception("Error in interactive mode")

    def _handle_command(self, command: str):
        """Handle special commands"""
        if command == '/help':
            console.print("""
[bold]Available Commands:[/bold]
  /help          - Show this help
  /status        - Show system status
  /config        - Show current configuration
  /graph         - Show workflow structure
  /adapters      - List loaded adapters
  quit/exit      - Exit interactive mode
            """)

        elif command == '/status':
            console.print(f"\n[bold]System Status:[/bold]")
            console.print(f"  Model: {self.config.model.model_name}")
            console.print(f"  Adapters: {len(self.adapter_registry.list_adapters()) if self.adapter_registry else 0} loaded")
            console.print(f"  Mode: {'Development' if self.config.dev_mode else 'Production'}")
            console.print()

        elif command == '/config':
            console.print(f"\n[bold]Current Configuration:[/bold]")
            console.print(f"  Max Iterations: {self.config.workflow.max_iterations}")
            console.print(f"  Quality Threshold: {self.config.workflow.quality_threshold}")
            console.print(f"  Mattermost: {'Enabled' if self.config.mattermost.enabled else 'Disabled'}")
            console.print()

        elif command == '/graph':
            print_graph_structure(self.graph)

        elif command == '/adapters':
            console.print(f"\n[bold]Loaded Adapters:[/bold]")
            for name in (self.adapter_registry.list_adapters() if self.adapter_registry else []):
                console.print(f"  • {name}")
            console.print()

        else:
            console.print(f"[yellow]Unknown command: {command}[/yellow]")
            console.print("Type /help for available commands\n")


def main():
    """CLI entry point"""
    parser = argparse.ArgumentParser(description="Multi-Agent System with Iterative Refinement")

    parser.add_argument("request", nargs="*", help="User request (omit for interactive mode)")
    parser.add_argument("--max-iterations", "-m", type=int, help="Maximum refinement iterations")
    parser.add_argument("--threshold", "-t", type=int, help="Quality threshold (0-100)")
    parser.add_argument("--stream", "-s", action="store_true", help="Stream output in real-time")
    parser.add_argument("--dev", action="store_true", help="Use development config")
    parser.add_argument("--show-graph", action="store_true", help="Show graph structure and exit")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run Paperclip bridge (Slack/Mattermost → Paperclip)")
    parser.add_argument("--doctor", action="store_true", help="Run diagnostic checks and exit")
    parser.add_argument("--heartbeat", action="store_true", help="Run one Paperclip heartbeat cycle and exit")
    parser.add_argument("--spending-status", action="store_true", help="Show spending tracker status and exit")
    parser.add_argument("--spending-reset", action="store_true", help="Reset the spending circuit breaker and exit")

    args = parser.parse_args()

    # Create config
    config = get_dev_config() if args.dev else get_production_config()

    # Auto-discover hardware and compute resource plan
    from .resource_discovery import discover_system
    from .resource_allocator import compute_resource_plan
    from .sandbox.config import SandboxConfig

    profile = discover_system()
    plan = compute_resource_plan(profile)

    # Build sandbox config from resource plan, then apply env var overrides
    config.sandbox = SandboxConfig.from_resource_plan(plan)
    config.sandbox.apply_env_overrides()

    # Spending tracker status/reset
    if args.spending_status or args.spending_reset:
        from .spending_tracker import SpendingTracker
        tracker = SpendingTracker(
            db_path=config.spending.db_path,
            window_seconds=config.spending.window_seconds,
            max_cents_per_window=config.spending.max_cents_per_window,
            max_heartbeats_per_window=config.spending.max_heartbeats_per_window,
            max_consecutive_non_idle=config.spending.max_consecutive_non_idle,
            cooldown_seconds=config.spending.cooldown_seconds,
            max_cooldown_seconds=config.spending.max_cooldown_seconds,
            retention_days=config.spending.retention_days,
        )
        if args.spending_reset:
            tracker.reset()
            console.print("[bold green]Circuit breaker reset to CLOSED[/bold green]")
            sys.exit(0)
        status = tracker.get_status()
        console.print(f"\n[bold]Spending Tracker Status[/bold]")
        console.print(f"  Window:              {status.window_seconds}s")
        console.print(f"  Cost (window):       {status.total_cost_cents}c / {tracker.max_cents_per_window}c")
        console.print(f"  Heartbeats (window): {status.non_idle_heartbeats} non-idle / {tracker.max_heartbeats_per_window} max")
        console.print(f"  Consecutive streak:  {status.consecutive_non_idle} / {tracker.max_consecutive_non_idle} max")
        console.print(f"  Circuit breaker:     {status.breaker.state.value}")
        if status.breaker.state.value != "closed":
            console.print(f"    Reason:   {status.breaker.reason}")
            console.print(f"    Trips:    {status.breaker.trip_count}")
            console.print(f"    Retry in: {status.breaker.retry_after_seconds}s")
        console.print()
        sys.exit(0)

    # Heartbeat mode — run one Paperclip heartbeat and exit
    if args.heartbeat:
        from .heartbeat import run_heartbeat
        setup_logging(config)
        config.paperclip.enabled = True
        result = run_heartbeat(config)
        # Write structured output for the adapter to parse
        if config.paperclip.output_format == "json":
            print(result.to_json())
        else:
            print(result.summary)
        sys.exit(result.exit_code)

    # Doctor mode — run diagnostics and exit
    if args.doctor:
        from .doctor import run_doctor
        report = run_doctor(config)
        print(report.format())
        sys.exit(1 if report.fail_count else 0)

    # Daemon mode - run always-on service
    if args.daemon:
        from .daemon import run_daemon
        setup_logging(config)
        console.print("\n[bold blue]🤖 Starting Genesia Daemon Mode[/bold blue]")
        console.print("Monitoring Slack/Mattermost for @mentions...\n")
        run_daemon(config)
        return

    # Initialize system
    system = MultiAgentSystem(config)
    system.initialize()

    # Show graph structure if requested
    if args.show_graph:
        print_graph_structure(system.graph)
        return

    # Run mode
    if args.request:
        # Single request mode
        request = " ".join(args.request)
        system.run(
            request,
            max_iterations=args.max_iterations,
            quality_threshold=args.threshold,
            stream=args.stream
        )
    else:
        # Interactive mode
        system.interactive_mode()


if __name__ == "__main__":
    main()
