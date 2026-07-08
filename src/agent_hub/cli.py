"""CLI entry point for the Agent Hub."""

from __future__ import annotations

import argparse
import logging
import os
import sys


from .log_config import configure_logging


def _setup_logging(verbose: bool, level: str | None = None) -> None:
    configure_logging(level or ("DEBUG" if verbose else "INFO"))


def _run_chat(model: str) -> None:
    from .orchestrator import HubOrchestrator

    orch = HubOrchestrator(model=model)
    registry = orch.registry

    if registry:
        print(f"Agent Hub ready — {len(registry)} agent(s) registered.")
        for spec in registry:
            print(f"  • {spec.name} ({spec.id}): {spec.purpose}")
    else:
        print("Agent Hub ready — no agents registered yet.")

    print("Commands: /new (reset session), /agents (list agents), /quit or Ctrl-C to exit.\n")

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


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

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

    args = parser.parse_args(_normalize_args(sys.argv[1:]))

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
