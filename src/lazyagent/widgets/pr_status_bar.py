from __future__ import annotations

from textual.widgets import Static

from lazyagent.models import PrInfo


class PrStatusBar(Static):
    """Shows PR status, review state, and CI summary below the sidebar."""

    DEFAULT_CSS = """
    PrStatusBar {
        height: auto;
        min-height: 5;
        max-height: 9;
        width: 1fr;
        border: solid $secondary;
        border-title-color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", markup=True, **kwargs)

    def on_mount(self) -> None:
        self.border_title = "PR"

    def update_pr_info(self, pr_info: PrInfo | None) -> None:
        """Render PR info or show 'No PR'."""
        if pr_info is None:
            self.update("[dim]No PR[/dim]")
            return

        lines: list[str] = []

        # Line 1: PR number + state
        state = pr_info.state
        if state == "OPEN":
            state_str = "[green]OPEN[/green]"
        elif state == "MERGED":
            state_str = "[magenta]MERGED[/magenta]"
        elif state == "CLOSED":
            state_str = "[red]CLOSED[/red]"
        else:
            state_str = f"[dim]{state}[/dim]"
        lines.append(f"[bold]#{pr_info.number}[/bold] {state_str}")

        # Line 2: title (truncated)
        title = pr_info.title
        if len(title) > 34:
            title = title[:31] + "\u2026"
        lines.append(title)

        # Line 3: CI checks
        overall = pr_info.overall_status
        if overall == "pass":
            checks_str = f"[green]{pr_info.checks_summary}[/green]"
        elif overall == "fail":
            checks_str = f"[red]{pr_info.checks_summary}[/red]"
        elif overall == "pending":
            checks_str = f"[yellow]{pr_info.checks_summary}[/yellow]"
        else:
            checks_str = f"[dim]{pr_info.checks_summary}[/dim]"
        lines.append(f"CI: {checks_str}")

        # Line 4: review decision (if set)
        rd = pr_info.review_decision
        if rd == "APPROVED":
            lines.append("[green]Approved[/green]")
        elif rd == "CHANGES_REQUESTED":
            lines.append("[red]Changes requested[/red]")
        elif rd == "REVIEW_REQUIRED":
            lines.append("[yellow]Review required[/yellow]")

        # Conflict warning (only when conflicting)
        if pr_info.mergeable == "CONFLICTING":
            lines.append("[bold red]Conflicts[/bold red]")

        # URL (clickable via @click action)
        if pr_info.url:
            short_url = pr_info.url
            for prefix in ("https://github.com/", "https://", "http://"):
                if short_url.startswith(prefix):
                    short_url = short_url[len(prefix):]
                    break
            lines.append(f'[@click=app.open_pr_url("{pr_info.url}")]{short_url}[/]')

        self.update("\n".join(lines))
