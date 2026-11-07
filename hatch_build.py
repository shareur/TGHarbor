from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Keep Hatchling's forced VCS metadata outside the strict sdist boundary."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        force_include: dict[str, str] = build_data["force_include"]
        gitignore = str(Path(self.root, ".gitignore"))
        if force_include.get(gitignore) == ".gitignore":
            del force_include[gitignore]
