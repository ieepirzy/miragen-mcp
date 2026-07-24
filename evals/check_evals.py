"""Deterministic, no-LLM consistency check for the eval suite.

Run in CI (and locally) without an ``ANTHROPIC_API_KEY``. It asserts three
things, all derivable from files already in the repo:

  1. Every answer in ``eval.xml`` still equals what ``ground_truth`` derives
     from the fixtures (so a fixture edit can't silently invalidate an answer).
  2. Every tool an eval relies on is a real, **read-only** tool of the server
     (source of truth: the ``readOnlyHint`` in ``server.py``'s annotations) —
     this is how "no eval touches a mutating tool" is enforced without a live
     transcript.
  3. The suite has at least 10 evals and no duplicate ids.

Exit code 0 on success, 1 on any failure (with a readable report).
"""

from __future__ import annotations

import ast
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
SERVER_PY = REPO_ROOT / "server.py"
EVAL_XML = EVALS_DIR / "eval.xml"

sys.path.insert(0, str(EVALS_DIR))
import ground_truth  # noqa: E402  (local module)


def parse_evals(path: Path = EVAL_XML) -> list[dict]:
    """Parse eval.xml into a list of {id, question, answer, tools}."""
    root = ET.parse(path).getroot()
    evals = []
    for node in root.findall("eval"):
        tools_text = (node.findtext("tools") or "").replace(",", " ")
        evals.append(
            {
                "id": node.get("id"),
                "question": (node.findtext("question") or "").strip(),
                "answer": (node.findtext("answer") or "").strip(),
                "tools": tools_text.split(),
            }
        )
    return evals


def tool_readonly_map(server_source: str) -> dict[str, bool]:
    """Map every @mcp.tool(name=...) to whether its annotations set read_only=True.

    Reads the annotations directly from server.py so the read-only classification
    can never drift from the tools' declared MCP hints.
    """
    tree = ast.parse(server_source)
    result: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if not (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "tool"
            ):
                continue
            name = None
            annotations_call = None
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name = kw.value.value
                elif kw.arg == "annotations":
                    annotations_call = kw.value
            if name is None:
                continue
            read_only = False
            if isinstance(annotations_call, ast.Call):
                for kw in annotations_call.keywords:
                    if kw.arg == "read_only" and isinstance(kw.value, ast.Constant):
                        read_only = bool(kw.value.value)
            result[name] = read_only
    return result


def check() -> list[str]:
    """Return a list of problem strings; empty means the suite is consistent."""
    problems: list[str] = []
    evals = parse_evals()
    truth = ground_truth.compute_answers()
    readonly = tool_readonly_map(SERVER_PY.read_text())

    if len(evals) < 10:
        problems.append(f"expected at least 10 evals, found {len(evals)}")

    ids = [e["id"] for e in evals]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        problems.append(f"duplicate eval ids: {sorted(dupes)}")

    for e in evals:
        eid = e["id"]
        if eid not in truth:
            problems.append(f"[{eid}] no ground-truth derivation for this id")
        elif truth[eid] != e["answer"]:
            problems.append(
                f"[{eid}] answer drifted: eval.xml={e['answer']!r} but fixtures derive {truth[eid]!r}"
            )
        for tool in e["tools"]:
            if tool not in readonly:
                problems.append(f"[{eid}] references unknown tool {tool!r}")
            elif not readonly[tool]:
                problems.append(f"[{eid}] references mutating (non read-only) tool {tool!r}")
        if not e["tools"]:
            problems.append(f"[{eid}] declares no tools")

    return problems


def main() -> int:
    problems = check()
    evals = parse_evals()
    if problems:
        print("EVAL SUITE INCONSISTENT:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"OK: {len(evals)} evals, all answers derive from fixtures, all tools read-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
