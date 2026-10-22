"""Run the synthetic eval battery against the REAL configured model provider.

Uses the module's own environment (LLM_MODEL, LLM_API_BASE, LLM_API_KEY) with
synthetic tools and memory, so it spends real tokens and needs a key. It is NOT
run in CI: the CI-safe check is tests/test_evals_meta.py, which uses stubs.

Compare providers by running once per configuration with a distinct
--provider-label and diffing the --json outputs.

Exit codes: 0 pass rate >= --min-pass-rate, 1 below it, 2 not configured.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import get_settings
from evals import harness


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--cases", default=str(BACKEND / "evals" / "cases"), help="directory of case YAML files")
    p.add_argument("--only", action="append", default=[], metavar="ID", help="run only this case id (repeatable)")
    p.add_argument("--provider-label", default=None, help="label for this run (default: the configured model)")
    p.add_argument("--json", dest="json_path", default=None, metavar="PATH", help="write the full summary as JSON")
    p.add_argument("--min-pass-rate", type=float, default=1.0, help="exit 1 below this pass rate (default 1.0)")
    return p


def _print_table(summary: harness.Summary) -> None:
    print(f"{'case':44} {'pass':5} {'outcome':17} {'ms':>6} {'tok':>6}  tools / failures")
    for r in summary.per_case:
        tools = ",".join(c["name"] or "?" for c in r.tool_calls) or "-"
        print(
            f"{r.id:44} {'PASS' if r.passed else 'FAIL':5} {r.outcome:17} "
            f"{r.latency_ms:6d} {r.tokens:6d}  {tools}"
        )
        for failure in r.failures:
            print(f"{'':44}   - {failure}")
    print()
    print(f"provider:           {summary.label}")
    print(f"pass rate:          {summary.pass_rate:.1%}")
    print(f"tool-call accuracy: {summary.tool_call_accuracy:.1%}")
    print(f"citation rate:      {summary.citation_rate:.1%}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    if not settings.llm_model:
        print(
            "error: LLM_MODEL is not set. The eval CLI calls the real provider "
            "configured in the module environment (LLM_MODEL, LLM_API_BASE, "
            "LLM_API_KEY); set them and retry.",
            file=sys.stderr,
        )
        return 2

    cases = harness.load_cases(args.cases)
    if args.only:
        unknown = set(args.only) - {c.id for c in cases}
        if unknown:
            print(f"error: unknown case id(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c.id in set(args.only)]

    label = args.provider_label or settings.llm_model
    summary = asyncio.run(harness.run_all(cases, harness.LiveProvider(), label))
    _print_table(summary)
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(summary.to_dict(), indent=2, default=str), encoding="utf-8")
    return 0 if summary.pass_rate >= args.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(main())
