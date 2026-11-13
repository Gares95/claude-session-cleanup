"""Build, extract, and load the generated Agensi package for tests.

The package script is loaded from an extracted copy of the built ZIP, never
from the repository, so tests exercise exactly what would be uploaded.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILDER_PATH = os.path.join(ROOT, "tools", "build_agensi.py")
MODULE_NAMES = (
    "activity", "cli", "config", "deletion", "discovery", "errors", "fsutil", "native_retention",
    "policy", "processes", "render", "report", "transcript",
)


def load_file(path: str, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


_builder = None


def builder() -> types.ModuleType:
    global _builder
    if _builder is None:
        _builder = load_file(BUILDER_PATH, "build_agensi")
    return _builder


def build_and_extract(directory: str) -> tuple[str, str]:
    """Build the ZIP into directory/out and extract it into directory/extracted.

    Returns (zip path, extracted package root).
    """
    tool = builder()
    path = tool.build(os.path.join(directory, "out"))
    extracted = os.path.join(directory, "extracted")
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.filename.startswith("/") or ".." in info.filename.split("/"):
                raise AssertionError(f"unsafe name in the archive: {info.filename}")
        archive.extractall(extracted)
    return path, os.path.join(extracted, tool.TOP)


def load_script(package_root: str, name: str = "agensi_cleanup_sessions") -> types.ModuleType:
    return load_file(os.path.join(package_root, *builder().SCRIPT.split("/")), name)


class ModuleView(types.ModuleType):
    """One canonical module's names, read from and written to the generated script.

    Writes go to the script's globals, so ``mock.patch`` on a view patches
    what the script's own functions call.
    """

    def __init__(self, name: str, script: types.ModuleType, renames: dict[str, str]) -> None:
        super().__init__(name)
        object.__setattr__(self, "_script", script)
        object.__setattr__(self, "_renames", renames)

    def __getattr__(self, name: str):
        return getattr(self._script, self._renames.get(name, name))

    def __setattr__(self, name: str, value) -> None:
        setattr(self._script, self._renames.get(name, name), value)

    def __delattr__(self, name: str) -> None:
        delattr(self._script, self._renames.get(name, name))


def install_views(script: types.ModuleType, renames: dict[tuple[str, str], str]) -> None:
    """Make ``import claude_session_cleanup[.module]`` resolve to the generated script."""
    package = ModuleView("claude_session_cleanup", script, {})
    package.__dict__["__path__"] = []
    sys.modules["claude_session_cleanup"] = package
    for module in MODULE_NAMES:
        view = ModuleView(f"claude_session_cleanup.{module}", script,
                          {name: new for (owner, name), new in renames.items() if owner == module})
        sys.modules[view.__name__] = view
        package.__dict__[module] = view
