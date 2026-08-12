#!/usr/bin/env python3
"""Install optional runtime packages for analyze-exam-errors."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REQUIREMENTS = SCRIPT_DIR / "runtime_optional_requirements.txt"
PROFILE_GROUPS = {
    "core": [],
    "math": ["math"],
    "semantic": ["semantic-common", "semantic-usearch", "embedding-sentence-transformers"],
    "semantic-onnx": ["semantic-common", "semantic-usearch", "embedding-onnx"],
    "full": ["math", "semantic-common", "semantic-usearch", "embedding-sentence-transformers"],
    "full-onnx": ["math", "semantic-common", "semantic-usearch", "embedding-onnx"],
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_grouped_requirements(path: str | Path) -> dict[str, list[str]]:
    current_group: str | None = None
    grouped: dict[str, list[str]] = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1].strip()
            grouped.setdefault(current_group, [])
            continue
        if current_group is None:
            raise ValueError("requirements file must start with a [group] header")
        grouped[current_group].append(line)
    return grouped


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def build_install_plan(
    profile: str = "full",
    requirements_file: str | Path = DEFAULT_REQUIREMENTS,
    python_executable: str | None = None,
    upgrade: bool = False,
) -> dict[str, Any]:
    grouped = parse_grouped_requirements(requirements_file)
    if profile not in PROFILE_GROUPS:
        raise ValueError(f"unsupported profile: {profile}")
    groups = PROFILE_GROUPS[profile]
    missing = [group for group in groups if group not in grouped]
    if missing:
        raise ValueError(f"requirements file is missing groups: {', '.join(missing)}")
    packages: list[str] = []
    for group in groups:
        packages.extend(grouped[group])
    packages = _ordered_unique(packages)
    python_bin = python_executable or sys.executable
    command = [python_bin, "-m", "pip", "install"]
    if upgrade:
        command.append("--upgrade")
    command.extend(packages)
    return {
        "profile": profile,
        "groups": groups,
        "requirements_file": str(Path(requirements_file).resolve()),
        "packages": packages,
        "command": command,
        "notes": [
            "建议在项目专用虚拟环境中安装，避免改变共享 Python 环境。",
            "安装只补运行时依赖，不会自动下载句向量模型文件。",
            "启用 sentence-transformers 或 ONNX 时，检索命令仍需提供本地 --model-path、--model-license 和批准的 --model-sha256。",
            "安装完成后运行 `python scripts/exam_error_cli.py capabilities` 复核是否已退出降级模式。",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install optional runtime packages for analyze-exam-errors."
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILE_GROUPS),
        default="full",
        help="runtime profile to install",
    )
    parser.add_argument(
        "--requirements-file",
        default=str(DEFAULT_REQUIREMENTS),
        help="grouped requirements file",
    )
    parser.add_argument("--python", default=sys.executable, help="Python executable to use")
    parser.add_argument("--dry-run", action="store_true", help="print the install plan only")
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="explicitly allow pip to upgrade packages in the selected environment",
    )
    args = parser.parse_args(argv)

    plan = build_install_plan(
        profile=args.profile,
        requirements_file=args.requirements_file,
        python_executable=args.python,
        upgrade=args.upgrade,
    )
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if not plan["packages"]:
        print(json.dumps({**plan, "installed": False, "reason": "core profile has no optional packages"}, ensure_ascii=False, indent=2))
        return 0

    subprocess.run(plan["command"], check=True)
    result = {**plan, "installed": True}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Installed optional runtime profile:", plan["profile"])
        print("Packages:")
        for package in plan["packages"]:
            print(" -", package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
