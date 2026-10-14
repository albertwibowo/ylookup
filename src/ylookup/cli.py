"""Click CLI: ylookup build / ylookup update / ylookup run."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ylookup.envutil import load_dotenv


@click.group()
@click.version_option(package_name="ylookup")
def main() -> None:
    """YLookup DIU workflow builder (build), updater (update), and runner (run)."""
    load_dotenv()


@main.command("build")
@click.option("--input", "input_xlsx", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--pdf", "pdf_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--target", "target_xlsx", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_workflow", required=True, type=click.Path(dir_okay=False))
@click.option("--context", "context_path", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="On ambiguity, print questions and exit 2 (no prompts).",
)
def build_cmd(
    input_xlsx: str,
    pdf_path: str,
    target_xlsx: str,
    out_workflow: str,
    context_path: str | None,
    non_interactive: bool,
) -> None:
    """Learn a workflow from input + target DIU (+ bank PDF).

    When OPENROUTER_API_KEY is set, LLM reasoning tokens (if the model returns
    them) are streamed to stderr during propose/patch.
    """
    from ylookup.builder import build_workflow

    report = build_workflow(
        input_xlsx=input_xlsx,
        pdf_path=pdf_path,
        target_xlsx=target_xlsx,
        out_workflow=out_workflow,
        context_path=context_path,
        non_interactive=non_interactive,
    )
    sys.exit(report.exit_code)


@main.command("update")
@click.option("--workflow", "workflow_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--input", "input_xlsx", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--pdf", "pdf_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--target", "target_xlsx", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_workflow", required=True, type=click.Path(dir_okay=False))
@click.option("--context", "context_path", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="On ambiguity, print questions and exit 2 (no prompts).",
)
def update_cmd(
    workflow_path: str,
    input_xlsx: str,
    pdf_path: str,
    target_xlsx: str,
    out_workflow: str,
    context_path: str | None,
    non_interactive: bool,
) -> None:
    """Adapt an existing workflow to a renamed/reordered workbook (+ target DIU).

    When OPENROUTER_API_KEY is set, LLM reasoning tokens (if the model returns
    them) are streamed to stderr during adapt/patch.
    """
    from ylookup.updater import update_workflow

    report = update_workflow(
        workflow_path=workflow_path,
        input_xlsx=input_xlsx,
        pdf_path=pdf_path,
        target_xlsx=target_xlsx,
        out_workflow=out_workflow,
        context_path=context_path,
        non_interactive=non_interactive,
    )
    sys.exit(report.exit_code)


@main.command("run")
@click.option("--workflow", "workflow_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--input", "input_xlsx", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--pdf", "pdf_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "output_xlsx", required=True, type=click.Path(dir_okay=False))
def run_cmd(
    workflow_path: str,
    input_xlsx: str,
    pdf_path: str,
    output_xlsx: str,
) -> None:
    """Execute a saved workflow; never prompts. Writes DIU only."""
    from ylookup.runner import run_saved_workflow

    result = run_saved_workflow(
        workflow_path,
        input_xlsx=input_xlsx,
        pdf_path=pdf_path,
        output_xlsx=output_xlsx,
    )
    if result.errors:
        for err in result.errors:
            click.echo(f"error: {err}", err=True)
        sys.exit(1)
    if result.output_path:
        click.echo(f"Wrote DIU → {result.output_path}")
        click.echo(f"DIU rows: {len(result.diu_rows)} (investors={result.investor_count})")
    sys.exit(0)


if __name__ == "__main__":
    main()
