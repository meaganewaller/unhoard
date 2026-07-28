"""Shared rich Console instances and small styling helpers, so every command
gets consistent colors and behavior (including automatic no-color-when-piped)
instead of each one deciding independently."""
from __future__ import annotations

from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def print_success(message: str) -> None:
    console.print(f"[green]✓[/green] {message}")


def print_warning(message: str) -> None:
    err_console.print(f"[yellow]warning:[/yellow] {message}")


def print_error(message: str) -> None:
    err_console.print(f"[red]error:[/red] {message}")
