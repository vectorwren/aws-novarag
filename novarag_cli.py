#!/usr/bin/env python3
"""NovaRAG CLI - Interactive CLI for querying Pydantic AI documentation."""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional
import subprocess
import requests
from dotenv import load_dotenv

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import Condition
from prompt_toolkit.application import run_in_terminal

# Load environment variables
load_dotenv()

# Default API URL - can be overridden by env var or CLI arg
DEFAULT_API_URL = os.getenv("NOVARAG_API_URL", None)

# History file path
HISTORY_PATH = Path.home() / ".novarag_history"

# Commands for auto-completion
COMMANDS = ["/quit", "/exit", "/q", "/stats", "/health", "/clear", "/help"]


def get_ecs_ip() -> Optional[str]:
    """Auto-discover ECS task IP if running in AWS environment."""
    try:
        cluster = os.getenv("ECS_CLUSTER_NAME", "nova-rag-cluster")
        region = os.getenv("AWS_REGION", "ap-southeast-2")

        result = subprocess.run(
            ["aws", "ecs", "list-tasks", "--cluster", cluster, "--region", region, "--query", "taskArns[0]", "--output", "text"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        task_arn = result.stdout.strip()
        result = subprocess.run(
            ["aws", "ecs", "describe-tasks", "--cluster", cluster, "--tasks", task_arn, "--region", region,
             "--query", "tasks[0].attachments[0].details[?name==`networkInterfaceId`].value", "--output", "text"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None

        eni = result.stdout.strip()
        result = subprocess.run(
            ["aws", "ec2", "describe-network-interfaces", "--network-interface-ids", eni, "--region", region,
             "--query", "NetworkInterfaces[0].Association.PublicIp", "--output", "text"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None

        return f"http://{result.stdout.strip()}:8000"
    except Exception:
        return None


# Auto-discover ECS IP if not set
if DEFAULT_API_URL is None:
    discovered_ip = get_ecs_ip()
    DEFAULT_API_URL = discovered_ip if discovered_ip else "http://localhost:8000"


# ANSI color codes for print statements (prompt_toolkit handles the prompt colors)
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Foreground colors
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"


class Emoji:
    SEARCH = "🔍"
    INFO = "ℹ️"
    SUCCESS = "✅"
    WARNING = "⚠️"
    ERROR = "❌"
    ROCKET = "🚀"
    CHART = "📊"
    CLOCK = "⏱️"
    MONEY = "💰"
    BRAIN = "🧠"
    CHAT = "💬"
    DOCUMENT = "📄"
    TOOL = "🛠️"


# Prompt style
STYLE = Style.from_dict({
    "prompt": "ansigreen bold",
    "command": "ansicyan",
})


class NovaRAGCLI:
    """CLI for NovaRAG service."""

    def __init__(self, api_url: str = DEFAULT_API_URL, verbose: bool = False):
        self.api_url = api_url.rstrip("/")
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.session_id = None

        # Create prompt session with history and auto-completion
        self.history = FileHistory(str(HISTORY_PATH))
        self.completer = WordCompleter(COMMANDS, ignore_case=True)
        self.session_prompt = PromptSession(
            history=self.history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=self.completer,
            style=STYLE,
            enable_history_search=True,
        )

    def _print(self, message: str, color: str = Colors.RESET, emoji: str = ""):
        """Print a colored message with optional emoji."""
        print(f"{color}{emoji} {message}{Colors.RESET}")

    def _print_header(self, text: str):
        """Print a section header."""
        print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 60}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.CYAN}{text:^60}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 60}{Colors.RESET}\n")

    def _print_stats(self, response_data: dict):
        """Print query statistics."""
        stats = {
            "Latency": f"{response_data.get('latency_ms', 0):,} ms",
            "Input Tokens": f"{response_data.get('input_tokens', 0):,}",
            "Output Tokens": f"{response_data.get('output_tokens', 0):,}",
            "Total Tokens": f"{response_data.get('total_tokens', 0):,}",
            "Estimated Cost": f"${response_data.get('estimated_cost_usd', 0):.6f}",
        }

        tools_used = response_data.get('tools_used', [])

        print(f"\n{Colors.BOLD}{Emoji.CHART} Query Statistics:{Colors.RESET}")
        for key, value in stats.items():
            emoji_map = {
                "Latency": Emoji.CLOCK,
                "Input Tokens": Emoji.DOCUMENT,
                "Output Tokens": Emoji.DOCUMENT,
                "Total Tokens": Emoji.BRAIN,
                "Estimated Cost": Emoji.MONEY,
            }
            emoji = emoji_map.get(key, "")
            print(f"  {Colors.DIM}•{Colors.RESET} {key:18}: {Colors.GREEN}{value}{Colors.RESET}")

        if tools_used:
            print(f"\n{Colors.BOLD}{Emoji.TOOL} Tools Used (in order):{Colors.RESET}")
            for i, tool in enumerate(tools_used, 1):
                formatted_tool = tool.replace('_', ' ').title()
                print(f"  {Colors.DIM}{i}.{Colors.RESET} {Colors.CYAN}{formatted_tool}{Colors.RESET}")
        else:
            print(f"\n{Colors.DIM}No tools were used for this query.{Colors.RESET}")

    def _print_answer(self, answer: str):
        """Print the answer with formatting."""
        start_tag = "<thinking>"
        end_tag = "</thinking>"

        start_idx = answer.find(start_tag)
        while start_idx != -1:
            end_idx = answer.find(end_tag, start_idx)
            if end_idx != -1:
                answer = answer[:start_idx] + answer[end_idx + len(end_tag):]
            else:
                answer = answer[:start_idx]
            start_idx = answer.find(start_tag)

        clean_answer = answer.strip()

        print(f"\n{Colors.BOLD}{Emoji.CHAT} Answer:{Colors.RESET}")
        print(f"{Colors.BLUE}{'─' * 60}{Colors.RESET}\n")
        print(clean_answer)
        print(f"\n{Colors.BLUE}{'─' * 60}{Colors.RESET}")

    def _print_help(self):
        """Print help information."""
        print(f"\n{Colors.BOLD}Available Commands:{Colors.RESET}")
        print(f"  {Colors.CYAN}/help{Colors.RESET}     - Show this help message")
        print(f"  {Colors.CYAN}/stats{Colors.RESET}    - Show overall statistics")
        print(f"  {Colors.CYAN}/health{Colors.RESET}   - Check service health")
        print(f"  {Colors.CYAN}/clear{Colors.RESET}    - Clear the screen")
        print(f"  {Colors.CYAN}/quit{Colors.RESET}     - Exit the CLI")
        print(f"\n{Colors.DIM}Tips:{Colors.RESET}")
        print(f"  • Use {Colors.YELLOW}↑/↓ arrows{Colors.RESET} to navigate history")
        print(f"  • Use {Colors.YELLOW}Ctrl+R{Colors.RESET} to search history")
        print(f"  • Use {Colors.YELLOW}Tab{Colors.RESET} to autocomplete commands")
        print(f"  • Type anything else to query the RAG system\n")

    def health_check(self) -> bool:
        """Check if the API is healthy."""
        try:
            response = self.session.get(f"{self.api_url}/health", timeout=10)
            if response.status_code == 200:
                data = response.json()
                self._print("Service is healthy!", Colors.GREEN, Emoji.SUCCESS)
                if self.verbose:
                    print(f"  Version: {data.get('version', 'unknown')}")
                    print(f"  Database: {data.get('database', 'unknown')}")
                return True
            return False
        except Exception as e:
            self._print(f"Health check failed: {e}", Colors.RED, Emoji.ERROR)
            return False

    def get_stats(self) -> dict:
        """Get overall service statistics."""
        try:
            response = self.session.get(f"{self.api_url}/stats", timeout=10)
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            self._print(f"Failed to get stats: {e}", Colors.RED, Emoji.ERROR)
        return {}

    def query(self, question: str, show_stats: bool = True) -> dict:
        """Query the NovaRAG service."""
        self._print("Querying NovaRAG...", Colors.BLUE, Emoji.SEARCH)

        start_time = time.time()
        try:
            response = self.session.post(
                f"{self.api_url}/query",
                json={"question": question},
                timeout=120
            )
            elapsed = time.time() - start_time

            if response.status_code == 200:
                data = response.json()
                self._print(f"Answer received in {elapsed:.2f}s", Colors.GREEN, Emoji.SUCCESS)

                self._print_answer(data.get("answer", "No answer received."))

                if show_stats:
                    self._print_stats(data)

                return data
            else:
                self._print(
                    f"Query failed with status {response.status_code}: {response.text}",
                    Colors.RED,
                    Emoji.ERROR
                )
                return {}
        except requests.Timeout:
            self._print("Request timed out", Colors.RED, Emoji.ERROR)
            return {}
        except Exception as e:
            self._print(f"Query failed: {e}", Colors.RED, Emoji.ERROR)
            return {}

    def interactive_mode(self):
        """Run the CLI in interactive mode."""
        self._print_header(f"{Emoji.ROCKET} NovaRAG Interactive Mode")

        # Health check first
        if not self.health_check():
            self._print("Service is not available. Exiting.", Colors.RED, Emoji.ERROR)
            return

        # Show overall stats
        stats = self.get_stats()
        if stats:
            print(f"\n{Colors.DIM}Overall Statistics:{Colors.RESET}")
            print(f"  Total Queries: {Colors.CYAN}{stats.get('total_queries', 0):,}{Colors.RESET}")
            print(f"  Avg Latency:   {Colors.CYAN}{stats.get('average_latency_ms', 0):.0f} ms{Colors.RESET}")
            print(f"  Total Cost:    {Colors.GREEN}${stats.get('total_cost_usd', 0):.4f}{Colors.RESET}\n")

        print(f"{Colors.DIM}Type your questions about Pydantic AI below.{Colors.RESET}")
        print(f"{Colors.DIM}Type {Colors.CYAN}/help{Colors.RESET} for available commands.\n")

        while True:
            try:
                user_input = self.session_prompt.prompt(
                    HTML('<b><style fg="green">nova&gt;</style></b> '),
                )

                if not user_input.strip():
                    continue

                # Handle commands
                cmd = user_input.lower().strip()

                if cmd in ["/quit", "/exit", "/q"]:
                    self._print("Goodbye!", Colors.CYAN, Emoji.INFO)
                    break
                elif cmd == "/stats":
                    stats = self.get_stats()
                    if stats:
                        self._print_stats(stats)
                    continue
                elif cmd == "/health":
                    self.health_check()
                    continue
                elif cmd == "/clear":
                    os.system("clear" if os.name != "nt" else "cls")
                    continue
                elif cmd == "/help":
                    self._print_help()
                    continue

                # Regular query
                self.query(user_input)
                print()

            except KeyboardInterrupt:
                print(f"\n{Colors.YELLOW}Interrupted. Type /quit to exit.{Colors.RESET}")
            except EOFError:
                self._print("\nGoodbye!", Colors.CYAN, Emoji.INFO)
                break


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="NovaRAG CLI - Query Pydantic AI documentation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode
  python novarag_cli.py

  # Single query
  python novarag_cli.py -q "How do I install Pydantic AI?"

  # Query with stats disabled
  python novarag_cli.py -q "What is Pydantic AI?" --no-stats

  # Use custom API URL
  python novarag_cli.py -q "How to create an agent?" --url http://localhost:8000

Commands in interactive mode:
  /help     Show available commands
  /stats    Show overall statistics
  /health   Check service health
  /clear    Clear the screen
  /quit     Exit the CLI

Features:
  • Arrow keys for navigation and editing
  • Tab completion for commands
  • Ctrl+R for reverse history search
  • Persistent command history
        """
    )

    parser.add_argument(
        "-q", "--query",
        help="Single query to execute (non-interactive mode)",
        type=str
    )
    parser.add_argument(
        "--url",
        help=f"API URL (default: {DEFAULT_API_URL})",
        type=str,
        default=DEFAULT_API_URL
    )
    parser.add_argument(
        "--no-stats",
        help="Disable statistics display after each query",
        action="store_true"
    )
    parser.add_argument(
        "-v", "--verbose",
        help="Enable verbose output",
        action="store_true"
    )

    args = parser.parse_args()

    # Create CLI instance
    cli = NovaRAGCLI(api_url=args.url, verbose=args.verbose)

    if args.query:
        # Single query mode
        cli.query(args.query, show_stats=not args.no_stats)
    else:
        # Interactive mode
        cli.interactive_mode()


if __name__ == "__main__":
    main()
