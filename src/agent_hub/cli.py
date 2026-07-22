"""CLI entry point for the Agent Hub."""

from __future__ import annotations

import argparse
import os
import sys

from .log_config import configure_logging

_HELP_TEXT = (
    "Send a plain message to dispatch it to a specialist agent.\n"
    "Commands: /help, /new (reset session), /agents (list agents), "
    "/status, /last, /learn, /memory, /forget, /learn-mode [on|off], "
    "/project [<path>|clear] (set/show/clear the target project for specialists), "
    "/stop, /approve, /reject, /quit or Ctrl-C to exit.\n"
)


def _setup_logging(verbose: bool, level: str | None = None) -> None:
    configure_logging(level or ("DEBUG" if verbose else "INFO"))


def _run_chat(model: str) -> None:
    from .orchestrator import HubOrchestrator
    from .startup_health import ensure_healthy_startup

    ensure_healthy_startup("chat")
    orch = HubOrchestrator(model=model)
    orch.set_learning_notifier(lambda message: print(f"\n{message}\n"))
    registry = orch.registry

    if registry:
        print(f"Agent Hub ready — {len(registry)} agent(s) registered.")
        for spec in registry:
            print(f"  • {spec.name} ({spec.id}): {spec.purpose}")
    else:
        print("Agent Hub ready — no agents registered yet.")

    print(_HELP_TEXT)

    while True:
        try:
            text = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye.")
            break

        if not text:
            continue

        if text == "/quit":
            print("Bye.")
            break

        if text == "/help":
            print(_HELP_TEXT)
            continue

        if text == "/new":
            orch.new_session()
            print("New session started.")
            continue

        if text == "/agents":
            if not orch.registry:
                print("No agents registered yet.")
            else:
                for spec in orch.registry:
                    print(f"  {spec.name} ({spec.id}): {spec.purpose}")
            continue

        if text == "/status":
            print(f"\nHub: {orch.current_run_status()}\n")
            continue

        if text == "/last":
            print(f"\nHub: {orch.last_run_status()}\n")
            continue

        if text == "/learn" or text.startswith("/learn "):
            value = text[len("/learn"):].strip()
            if not value:
                print("\nHub: Usage: /learn <instruction or fact>\n")
            else:
                print(f"\nHub: {orch.learn(value, source='cli')}\n")
            continue

        if text == "/memory":
            print(f"\nHub: {orch.memory()}\n")
            continue

        if text.startswith("/learn-mode"):
            arg = text[len("/learn-mode"):].strip().lower()
            if arg == "on":
                print(f"\nHub: {orch.set_learning_mode(True)}\n")
            elif arg == "off":
                print(f"\nHub: {orch.set_learning_mode(False)}\n")
            elif not arg:
                print(f"\nHub: {orch.learning_mode_status()}\n")
            else:
                print("\nHub: Usage: /learn-mode [on|off]\n")
            continue

        if text.startswith("/forget"):
            identifier = text[len("/forget"):].strip()
            print(f"\nHub: {orch.forget_learning(identifier)}\n")
            continue

        if text.startswith("/project"):
            arg = text[len("/project"):].strip()
            if not arg:
                print(f"\nHub: {orch.current_project_status()}\n")
            elif arg.lower() == "clear":
                print(f"\nHub: {orch.clear_current_project()}\n")
            else:
                print(f"\nHub: {orch.set_current_project(arg)}\n")
            continue

        if text == "/stop":
            print(f"\nHub: {orch.stop_current_task()}\n")
            continue

        if text == "/approve":
            try:
                reply = orch.approve_pending()
                print(f"\nHub: {reply}\n")
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
            continue

        if text.startswith("/reject"):
            reason = text[len("/reject"):].strip() or "Rejected by user"
            try:
                reply = orch.reject_pending(reason)
                print(f"\nHub: {reply}\n")
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
            continue

        pending = orch.pending_run()
        if pending is not None and pending.state == "waiting_clarification":
            try:
                reply = orch.provide_clarification(text)
                print(f"\nHub: {reply}\n")
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
            continue

        try:
            reply = orch.invoke(text)
            print(f"\nHub: {reply}\n")
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)


def _run_telegram() -> None:
    from .telegram_gateway import run_telegram

    run_telegram()


def _normalize_args(argv: list[str]) -> list[str]:
    normalized: list[str] = []
    non_global: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in {"--verbose", "-v", "--debug"}:
            normalized.append(arg)
            i += 1
            continue
        if arg == "--log-level":
            normalized.append(arg)
            if i + 1 < len(argv):
                normalized.append(argv[i + 1])
            i += 2
            continue
        non_global.append(arg)
        i += 1
    return normalized + non_global


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-hub", description="Agent Hub orchestrator")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--verbose", "-v", action="store_true", help="Enable info logging")
    group.add_argument("--debug", action="store_true", help="Enable debug logging")
    group.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level explicitly.",
    )

    sub = parser.add_subparsers(dest="command")

    chat_parser = sub.add_parser("chat", help="Interactive CLI chat (default)")
    chat_parser.add_argument("--model", default=os.getenv("HUB_MODEL", "gpt-4.1-mini"))

    sub.add_parser("telegram", help="Run the Telegram bot gateway")
    return parser


def main(argv: list[str] | None = None) -> None:
    from dotenv import load_dotenv

    load_dotenv()

    parser = build_parser()
    args = parser.parse_args(_normalize_args(argv if argv is not None else sys.argv[1:]))

    if args.debug:
        log_level = "DEBUG"
    elif args.verbose:
        log_level = "INFO"
    else:
        log_level = args.log_level or "INFO"

    _setup_logging(False, log_level)

    command = args.command or "chat"

    if command == "chat":
        model = getattr(args, "model", os.getenv("HUB_MODEL", "gpt-4.1-mini"))
        _run_chat(model)
    elif command == "telegram":
        _run_telegram()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
