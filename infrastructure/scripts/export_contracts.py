#!/usr/bin/env python
"""Export the backend's canonical enums to ``shared/`` as JSON and TypeScript.

    python infrastructure/scripts/export_contracts.py
    python infrastructure/scripts/export_contracts.py --check    # CI: fail if stale

Why this exists: status strings are the contract between three codebases (backend, web, mobile).
Hand-maintaining a TypeScript copy of ``CaptureSessionStatus`` guarantees it drifts, and the
drift shows up as a status the UI silently fails to render rather than as an error. Generating
from the single Python definition makes drift a build failure instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from enum import EnumMeta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
SHARED = REPO_ROOT / "shared"

sys.path.insert(0, str(BACKEND))

from app.core import enums as enum_module  # noqa: E402

#: Emitted in this order so the generated file is stable and diffs stay readable.
EXPORTED = [
    "Role",
    "ProjectStatus",
    "CaptureSessionStatus",
    "CaptureSource",
    "FileKind",
    "ProjectionType",
    "JobType",
    "JobStatus",
    "QualityVerdict",
    "DataProvenance",
    "MappingConfidence",
    "AnnotationPriority",
    "AnnotationStatus",
    "CaptureFrequencyStatus",
    "ReportType",
    "ReportFormat",
    "ReportStatus",
    "CameraTransport",
]

BANNER = (
    "// GENERATED FILE — do not edit.\n"
    "// Source: backend/app/core/enums.py\n"
    "// Regenerate: python infrastructure/scripts/export_contracts.py\n"
)


def collect() -> dict[str, list[str]]:
    contracts: dict[str, list[str]] = {}
    for name in EXPORTED:
        enum_class = getattr(enum_module, name, None)
        if not isinstance(enum_class, EnumMeta):
            raise SystemExit(f"{name} is not an enum in app.core.enums")
        contracts[name] = [str(member.value) for member in enum_class]
    return contracts


def render_json(contracts: dict[str, list[str]]) -> str:
    payload = {
        "_generated_from": "backend/app/core/enums.py",
        "_regenerate": "python infrastructure/scripts/export_contracts.py",
        "enums": contracts,
        "pipeline_order": [str(stage) for stage in enum_module.PIPELINE_ORDER],
        "role_rank": {str(role): rank for role, rank in enum_module.ROLE_RANK.items()},
    }
    return json.dumps(payload, indent=2) + "\n"


def render_typescript(contracts: dict[str, list[str]]) -> str:
    lines = [BANNER]
    for name, values in contracts.items():
        union = " | ".join(f"'{value}'" for value in values)
        lines.append(f"export type {name} = {union}\n")
        # A runtime array as well as the type: dropdowns, filters and badge maps all need to
        # enumerate the values, and a bare type union cannot be iterated.
        const_name = _screaming(name)
        entries = ", ".join(f"'{value}'" for value in values)
        lines.append(
            f"export const {const_name}: readonly {name}[] = [{entries}] as const\n"
        )
    lines.append(
        "export const PIPELINE_ORDER: readonly JobType[] = ["
        + ", ".join(f"'{stage}'" for stage in enum_module.PIPELINE_ORDER)
        + "] as const\n"
    )
    lines.append(
        "export const ROLE_RANK: Readonly<Record<Role, number>> = {"
        + ", ".join(f"{role}: {rank}" for role, rank in enum_module.ROLE_RANK.items())
        + "} as const\n"
    )
    return "\n".join(lines)


def _screaming(name: str) -> str:
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index > 0:
            out.append("_")
        out.append(char.upper())
    return "".join(out) + "_VALUES"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the generated files are out of date (for CI)",
    )
    args = parser.parse_args(argv)

    contracts = collect()
    targets = {
        SHARED / "contracts.json": render_json(contracts),
        SHARED / "contracts.ts": render_typescript(contracts),
    }

    SHARED.mkdir(parents=True, exist_ok=True)
    stale: list[Path] = []
    for path, content in targets.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == content:
            continue
        if args.check:
            stale.append(path)
        else:
            path.write_text(content, encoding="utf-8")
            print(f"wrote {path.relative_to(REPO_ROOT)}")

    if stale:
        print("Generated contracts are out of date:", file=sys.stderr)
        for path in stale:
            print(f"  {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        print(
            "Run: python infrastructure/scripts/export_contracts.py",
            file=sys.stderr,
        )
        return 1

    if args.check:
        print("Contracts are up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
