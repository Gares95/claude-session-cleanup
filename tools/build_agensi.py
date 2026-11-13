#!/usr/bin/env python3
"""Build the Agensi skill package from the canonical source.

Agensi runs each script of a skill on its own, from a temporary file, so a
script cannot import the rest of its package. This tool generates one
standalone script from ``claude_session_cleanup/`` and packs it with the
Agensi ``SKILL.md`` (``packaging/agensi/SKILL.md``), the safety model, and
the license into a deterministic ZIP:

    python3 tools/build_agensi.py            # writes dist/agensi/claude-session-cleanup-<version>.zip

The generated script is the canonical modules, concatenated in dependency
order and otherwise verbatim, except that:

- the package-internal imports (``from .x import y``) and the per-module
  ``from __future__ import annotations`` are removed (one future import
  heads the script);
- references through package-module aliases (``deletion.lock``,
  ``processes_module.scan``) become plain names (``lock``, ``scan``);
- top-level names are renamed with their module's name when more than one
  module defines them, or when a local name would hide them where another
  module refers to them (listed in the script's header).

The build verifies each section by comparing its syntax tree with the
canonical module's tree after exactly those changes, so any other
difference fails the build; checks with ``symtable`` that every rewritten
name still resolves to the global it meant and that no global lookup of a
module (a builtin, say) would now find another module's name; and refuses
imports outside a reviewed standard-library allowlist and calls that run
code or processes. Nothing
here touches Claude Code data, runs a process, or uses the network.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import os
import re
import symtable
import sys
import tokenize
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "claude_session_cleanup"
SOURCE_URL = "https://github.com/Gares95/claude-session-cleanup"
TOP = "claude-session-cleanup"
SCRIPT = "scripts/cleanup_sessions.py"

# Dependency order; checked against the package's own imports.
MODULES = (
    "__init__",
    "errors",
    "config",
    "fsutil",
    "transcript",
    "processes",
    "discovery",
    "policy",
    "activity",
    "native_retention",
    "deletion",
    "report",
    "render",
    "cli",
)
NOT_BUNDLED = ("__main__",)  # replaced by the script's own entry point

# Package files: (path in the package, source relative to the repository).
STATIC_FILES = (
    ("SKILL.md", "packaging/agensi/SKILL.md"),
    ("LICENSE", "LICENSE"),
    ("references/safety.md", "docs/safety-model.md"),
)

# Every module the generated script may import. A new import in the
# canonical source fails the build until it is reviewed and added here.
ALLOWED_IMPORTS = frozenset({
    "__future__", "argparse", "dataclasses", "datetime", "errno", "fcntl", "hashlib", "json",
    "os", "re", "stat", "sys", "typing", "unicodedata", "uuid",
})
FORBIDDEN_CALLS = frozenset({"exec", "eval", "compile", "__import__", "breakpoint", "input"})
FORBIDDEN_OS = re.compile(r"(system|popen|exec\w*|spawn\w*|posix_spawn\w*|fork\w*|startfile|chmod|chown|lchown|fchmod|fchown|symlink|link|truncate)\Z")

# Fixed ZIP metadata so identical sources give identical archives.
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644

FRONTMATTER_RE = re.compile(r"---\n(.*?)\n---\n", re.S)


class BuildError(Exception):
    pass


def read_text(relative: str) -> str:
    """A source file with LF line endings, whatever the checkout's line-ending setting."""
    with open(os.path.join(ROOT, relative), "rb") as handle:
        data = handle.read().replace(b"\r\n", b"\n")
    if b"\r" in data:
        raise BuildError(f"{relative}: stray carriage return")
    return data.decode("utf-8")


def version() -> str:
    tree = ast.parse(read_text(f"{PACKAGE}/__init__.py"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == ["__version__"]:
            return ast.literal_eval(node.value)
    raise BuildError("no __version__ in the package")


# -- analysis ----------------------------------------------------------------


def top_level_names(tree: ast.Module) -> set[str]:
    """Names a module binds at top level, other than through imports."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
            for child in ast.walk(node):
                if isinstance(child, (ast.FunctionDef, ast.ClassDef)):
                    names.add(child.name)
                elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                    names.add(child.id)
    return names


def is_internal_import(node: ast.stmt) -> bool:
    return isinstance(node, ast.ImportFrom) and (node.level > 0 or node.module == "__future__")


class Module:
    def __init__(self, name: str) -> None:
        self.name = name
        self.path = f"{PACKAGE}/{name}.py"
        self.source = read_text(self.path)
        self.sha256 = hashlib.sha256(self.source.encode("utf-8")).hexdigest()
        self.tree = ast.parse(self.source, self.path)
        self.defined = top_level_names(self.tree)
        self.aliases: dict[str, str] = {}  # local alias -> bundled module
        self.imported: dict[str, str] = {}  # local name -> (module, name) source module
        self.depends: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0 and node not in self.tree.body:
                raise BuildError(f"{self.path}:{node.lineno}: package import inside a block is not supported")
        for node in self.tree.body:
            if not (isinstance(node, ast.ImportFrom) and node.level > 0):
                continue
            if node.level != 1:
                raise BuildError(f"{self.path}:{node.lineno}: unsupported relative import")
            for alias in node.names:
                if node.module is None and alias.name in MODULES:
                    self.aliases[alias.asname or alias.name] = alias.name
                    self.depends.add(alias.name)
                else:
                    source = node.module or "__init__"
                    if alias.asname not in (None, alias.name):
                        raise BuildError(f"{self.path}:{node.lineno}: renamed import of {alias.name}")
                    self.imported[alias.name] = source
                    self.depends.add(source)


def plan_renames(modules: list[Module]) -> dict[tuple[str, str], str]:
    owners: dict[str, list[str]] = {}
    for module in modules:
        for name in module.defined:
            owners.setdefault(name, []).append(module.name)
    renames: dict[tuple[str, str], str] = {}
    for name, defining in owners.items():
        if len(defining) < 2:
            continue
        for module_name in defining:
            renames[(module_name, name)] = rename_for(module_name, name)
    return renames


def check_renames(modules: list[Module], renames: dict[tuple[str, str], str]) -> None:
    taken = {name for module in modules for name in module.defined}
    new_names = list(renames.values())
    for new in new_names:
        if new in taken or new_names.count(new) > 1:
            raise BuildError(f"cannot rename to {new}: the name is taken")


# -- rewriting ---------------------------------------------------------------


def bundled_name(renames: dict[tuple[str, str], str], module: str, name: str) -> str:
    return renames.get((module, name), name)


def local_renames(module: Module, renames: dict[tuple[str, str], str]) -> dict[str, str]:
    result = {name: new for (owner, name), new in renames.items() if owner == module.name}
    for name, source in module.imported.items():
        if (source, name) in renames:
            result[name] = renames[(source, name)]
    return result


class Expected(ast.NodeTransformer):
    """The canonical tree with exactly the documented changes applied."""

    def __init__(self, module: Module, renames: dict[tuple[str, str], str]) -> None:
        self.module = module
        self.renames = renames
        self.local = local_renames(module, renames)

    def visit_Module(self, node: ast.Module) -> ast.Module:
        node.body = [child for child in node.body if not is_internal_import(child)]
        self.generic_visit(node)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if isinstance(node.value, ast.Name) and node.value.id in self.module.aliases:
            target = self.module.aliases[node.value.id]
            return ast.copy_location(ast.Name(id=bundled_name(self.renames, target, node.attr), ctx=node.ctx), node)
        self.generic_visit(node)
        return node

    def visit_Name(self, node: ast.Name) -> ast.Name:
        if node.id in self.local:
            node.id = self.local[node.id]
        return node

    def _rename_def(self, node):
        if node.name in self.local:
            node.name = self.local[node.name]
        self.generic_visit(node)
        return node

    visit_FunctionDef = visit_ClassDef = _rename_def


def _tables(table: symtable.SymbolTable):
    yield table
    for child in table.get_children():
        yield from _tables(child)


def unbound_globals(module: Module) -> set[str]:
    """Names the canonical module looks up globally without binding them (builtins, normally)."""
    top = symtable.symtable(module.source, module.path, "exec")
    bound = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
    referenced = set()
    for table in _tables(top):
        for symbol in table.get_symbols():
            if symbol.is_referenced() and (table is top or symbol.is_global()):
                referenced.add(symbol.get_name())
    return referenced - bound


def shadowed(module: Module, text: str, renames: dict[tuple[str, str], str]) -> tuple[set[tuple[str, str]], list[str]]:
    """Introduced names that would not resolve to the intended global in the rewritten section.

    Returns alias targets to rename (a local variable has the same name, as in
    ``scan = processes_module.scan(...)``) and unfixable problems.
    """
    introduced: dict[str, tuple[str, str]] = {}
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in module.aliases:
            target = module.aliases[node.value.id]
            introduced[bundled_name(renames, target, node.attr)] = (target, node.attr)
    own = set(local_renames(module, renames).values())
    to_rename: set[tuple[str, str]] = set()
    problems: list[str] = []
    top = symtable.symtable(text, module.path, "exec")
    for table in _tables(top):
        for name in (set(introduced) | own) & {s.get_name() for s in table.get_symbols()}:
            symbol = table.lookup(name)
            if table is top:
                clash = name in introduced and (symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace())
            else:
                clash = symbol.is_local() or symbol.is_parameter() or symbol.is_free()
            if not clash:
                continue
            if name in introduced and name not in own:
                to_rename.add(introduced[name])
            else:
                problems.append(f"{module.path}: {name} is shadowed in {table.get_name()}")
    return to_rename, problems


def rename_for(module_name: str, name: str) -> str:
    if name.isupper():
        return f"{module_name.upper()}_{name}"
    if name.startswith("_"):
        return f"_{module_name}{name}"
    return f"{module_name}_{name}"


def rewrite(module: Module, renames: dict[tuple[str, str], str]) -> str:
    """Apply the documented changes to the module's text, keeping comments and layout."""
    source = module.source
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def offset(row: int, col: int) -> int:
        return starts[row - 1] + col

    removed: list[tuple[int, int]] = []
    removed_rows: set[int] = set()
    for node in module.tree.body:
        if is_internal_import(node):
            removed.append((starts[node.lineno - 1], starts[node.end_lineno]))
            removed_rows.update(range(node.lineno, node.end_lineno + 1))

    local = local_renames(module, renames)
    edits: list[tuple[int, int, str]] = []
    tokens = [t for t in tokenize.generate_tokens(io.StringIO(source).readline)
              if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT)]
    alias_re = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, module.aliases)) + r")\s*\.\s*(\w+)") \
        if module.aliases else None
    name_re = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, local)) + r")(?!\w)") if local else None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.start[0] in removed_rows:
            index += 1
            continue
        previous = tokens[index - 1].string if index else ""
        if token.type == tokenize.NAME and previous != ".":
            if (token.string in module.aliases and index + 2 < len(tokens)
                    and tokens[index + 1].string == "." and tokens[index + 2].type == tokenize.NAME):
                attr = tokens[index + 2]
                new = bundled_name(renames, module.aliases[token.string], attr.string)
                edits.append((offset(*token.start), offset(*attr.end), new))
                index += 3
                continue
            if token.string in local:
                edits.append((offset(*token.start), offset(*token.end), local[token.string]))
        elif token.type == tokenize.STRING and re.match(r"[A-Za-z]*[fF]", token.string):
            # Before Python 3.12 an f-string is a single token; its fields are
            # rewritten textually and checked by the syntax-tree comparison.
            text = token.string
            if alias_re is not None:
                text = alias_re.sub(lambda m: bundled_name(renames, module.aliases[m.group(1)], m.group(2)), text)
            if name_re is not None:
                text = name_re.sub(lambda m: local[m.group(1)], text)
            if text != token.string:
                edits.append((offset(*token.start), offset(*token.end), text))
        index += 1

    pieces = sorted([(s, e, "") for s, e in removed] + edits, reverse=True)
    text = source
    last_start = len(source) + 1
    for start, end, new in pieces:
        if end > last_start:
            raise BuildError(f"{module.path}: overlapping edits")
        text = text[:start] + new + text[end:]
        last_start = start
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip("\n") + "\n"

    expected = Expected(module, renames).visit(ast.parse(source))
    if ast.dump(expected) != ast.dump(ast.parse(text)):
        raise BuildError(f"{module.path}: the rewritten section does not match the canonical module")
    return text


# -- the script --------------------------------------------------------------


def check_order(modules: list[Module]) -> None:
    present = sorted(name[:-3] for name in os.listdir(os.path.join(ROOT, PACKAGE)) if name.endswith(".py"))
    if sorted(MODULES + NOT_BUNDLED) != present:
        raise BuildError(f"module list is out of date: package has {present}")
    seen: set[str] = set()
    for module in modules:
        missing = module.depends - seen
        if missing:
            raise BuildError(f"{module.path} needs {sorted(missing)} first; update MODULES")
        seen.add(module.name)


def check_script(text: str) -> None:
    """Refuse constructs a reviewed standalone script must not contain."""
    tree = ast.parse(text)
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                key = alias.asname or alias.name.split(".")[0]
                form = ast.dump(ast.Import(names=[alias])) if isinstance(node, ast.Import) else \
                    f"{node.module}:{alias.name}"
                if bindings.get(key, form) != form:
                    raise BuildError(f"conflicting imports of {key}")
                bindings[key] = form
    defined: dict[str, int] = {}
    for node in tree.body:
        for name in top_level_names(ast.Module(body=[node], type_ignores=[])):
            if name in bindings:
                raise BuildError(f"{name} is both imported and defined")
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and name in defined:
                raise BuildError(f"{name} is defined twice")
            defined[name] = node.lineno
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise BuildError(f"line {node.lineno}: relative import left in the script")
            modules = [node.module or ""]
        else:
            modules = []
        for name in modules:
            if name.split(".")[0] not in ALLOWED_IMPORTS:
                raise BuildError(f"line {node.lineno}: import of {name} is not allowed")
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id in FORBIDDEN_CALLS:
                raise BuildError(f"line {node.lineno}: call to {function.id}")
            if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name) \
                    and function.value.id == "os" and FORBIDDEN_OS.match(function.attr):
                raise BuildError(f"line {node.lineno}: call to os.{function.attr}")
            for keyword in node.keywords:
                if keyword.arg == "shell":
                    raise BuildError(f"line {node.lineno}: shell= argument")
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            if re.search(r"[A-Za-z0-9+/=]{200,}", node.value if isinstance(node.value, str) else node.value.decode("latin-1")):
                raise BuildError(f"line {node.lineno}: long encoded-looking literal")
    compile(text, SCRIPT, "exec", dont_inherit=True)


def generate() -> tuple[str, dict[tuple[str, str], str]]:
    """The standalone script, and the renamed top-level names ((module, name) -> name)."""
    modules = [Module(name) for name in MODULES]
    check_order(modules)
    renames = plan_renames(modules)
    for _ in range(10):
        check_renames(modules, renames)
        sections: dict[str, str] = {}
        needed: set[tuple[str, str]] = set()
        problems: list[str] = []
        for module in modules:
            sections[module.name] = rewrite(module, renames)
            more, trouble = shadowed(module, sections[module.name], renames)
            needed |= more
            problems += trouble
        if problems:
            raise BuildError("; ".join(problems))
        if not needed:
            break
        for owner, name in needed:
            renames[(owner, name)] = rename_for(owner, name)
    else:
        raise BuildError("renaming did not converge")
    bundle_globals = {bundled_name(renames, m.name, n): m.name for m in modules for n in m.defined}
    for module in modules:
        for name in sorted(unbound_globals(module)):
            owner = bundle_globals.get(name)
            if owner is not None and owner != module.name:
                raise BuildError(f"{module.path}: global lookup of {name} would find {owner}.{name} in the script")
    release = version()
    rename_lines = [f"#   {owner}.{name} -> {new}" for (owner, name), new in sorted(renames.items())]
    header = [
        "#!/usr/bin/env python3",
        f"# claude-session-cleanup {release}: standalone script of the Agensi skill package.",
        "#",
        "# GENERATED by tools/build_agensi.py from the canonical, MIT-licensed source in",
        f"# claude_session_cleanup/ at {SOURCE_URL}",
        "# Do not edit this file: change the canonical source and rebuild.",
        "#",
        "# Each section below is one module of the canonical package, verbatim except",
        "# that package-internal imports are removed, references through package-module",
        "# aliases (deletion.lock) are plain names (lock), and these top-level names",
        "# are renamed, because more than one module defines them or because a local",
        "# name would hide them where another module refers to them:",
        *rename_lines,
        "# The build checks each section's syntax tree against its canonical module.",
        "#",
        "# Standard library only. No network access, no subprocesses, no dynamic code.",
        "# Every run is a read-only preview unless --apply-plan is given.",
        "#",
        "# Copyright (c) 2026 Gares95. MIT License: see LICENSE in this package.",
        "",
        "from __future__ import annotations",
        "",
    ]
    parts = []
    for module in modules:
        banner = f"# {'-' * 75}\n# {module.path}  (sha256 {module.sha256})\n# {'-' * 75}\n"
        parts.append(banner + "\n" + sections[module.name])
    footer = '\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
    text = "\n".join(header) + "\n" + "\n\n".join(parts) + footer
    check_script(text)
    return text, renames


# -- the package -------------------------------------------------------------


def check_skill(text: str, release: str) -> None:
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise BuildError("packaging/agensi/SKILL.md must start with frontmatter")
    fields = dict(re.findall(r"^([a-z_]+):[ \t]*(.*)$", match.group(1), re.M))
    if fields.get("name") != TOP:
        raise BuildError(f"SKILL.md name must be {TOP}")
    if fields.get("version") != release:
        raise BuildError(f"SKILL.md version {fields.get('version')} does not match the package version {release}")
    for required in ("description", "summary", "docs_url"):
        if not fields.get(required):
            raise BuildError(f"SKILL.md has no {required}")


def package_files() -> list[tuple[str, bytes]]:
    release = version()
    files = [(SCRIPT, generate()[0])]
    for target, source in STATIC_FILES:
        files.append((target, read_text(source)))
    check_skill(dict(files)["SKILL.md"], release)
    return sorted((f"{TOP}/{path}", text.encode("utf-8")) for path, text in files)


def write_zip(path: str, files: list[tuple[str, bytes]]) -> None:
    temporary = path + ".partial"
    with zipfile.ZipFile(temporary, "w") as archive:
        for name, data in files:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = FILE_MODE << 16
            archive.writestr(info, data, compresslevel=9)
    os.replace(temporary, path)


def build(out_dir: str) -> str:
    files = package_files()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{TOP}-{version()}.zip")
    write_zip(path, files)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Agensi skill package.")
    parser.add_argument("--out", default=os.path.join(ROOT, "dist", "agensi"), help="output directory")
    args = parser.parse_args(argv)
    try:
        path = build(args.out)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    with open(path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            print(f"{info.file_size:>8}  {info.filename}")
    shown = os.path.relpath(path, ROOT) if path.startswith(ROOT + os.sep) else os.path.basename(path)
    print(f"{os.path.getsize(path):>8}  {shown}  sha256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
