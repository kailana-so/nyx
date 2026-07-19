"""Dependency existence checks against the real registries.

A model inventing `@react-native-macos/react-native` is not a mistake it can be
prompted out of — a fake package name is produced by the same machinery as a real
one, and the model has no signal that it guessed. But the ground truth is one HTTP
call away, so we take it: a manifest naming a package that does not exist is
rejected before it is ever written.

Network failures fail OPEN. A checker that blocks writes when you're offline is a
checker you'd disable.
"""
from __future__ import annotations
import json
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_TIMEOUT = 6

# The manifests check_manifest understands. Single source of truth — the write gate
# uses this to decide what to check, so the two never drift.
MANIFEST_FILENAMES = frozenset({
    "package.json", "pyproject.toml", "requirements.txt", "requirements-dev.txt",
})

# (registry url template, is-version-known predicate source)
_REGISTRIES = {
    "npm": "https://registry.npmjs.org/{name}",
    "pypi": "https://pypi.org/pypi/{name}/json",
}


def _fetch(url: str) -> dict | None:
    """Parsed JSON, or None for 404. Raises on network trouble (→ fail open)."""
    req = urllib.request.Request(url, headers={"User-Agent": "nyx"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def check_one(ecosystem: str, name: str, version: str = "") -> str:
    """'' if fine, otherwise a one-line problem description.

    ecosystem="auto" when the source didn't say which registry (a bare bullet in a
    spec). Then the package only has to exist in ONE of them — guessing npm and
    flagging `rich` because npm happens to host an unrelated stub is a false
    positive, and a checker that cries wolf gets turned off.
    """
    if ecosystem == "auto":
        results = [check_one(e, name, version) for e in _REGISTRIES]
        if any(r == "" for r in results):
            return ""
        return next((r for r in results if "version" in r), results[0])

    template = _REGISTRIES.get(ecosystem)
    if not template:
        return ""
    quoted = urllib.parse.quote(name, safe="")  # scoped names: @scope/pkg → @scope%2Fpkg
    try:
        data = _fetch(template.format(name=quoted))
    except Exception:
        return ""  # offline or registry down — do not block

    if data is None:
        return f"{name} — DOES NOT EXIST on {ecosystem}"

    # A real name with an invented version is the same failure wearing a hat.
    pinned = version.strip().lstrip("^~>=<").strip()
    if pinned and re.fullmatch(r"[\w.\-]+", pinned):
        known = data.get("versions") or data.get("releases") or {}
        if known and pinned not in known:
            newest = data.get("dist-tags", {}).get("latest") or data.get("info", {}).get("version", "?")
            return f"{name}@{version} — version does not exist (latest is {newest})"
    return ""


def _manifest_deps(path: Path, text: str) -> tuple[str, dict[str, str]]:
    """(ecosystem, {name: version}) for a manifest we understand — else ('', {})."""
    name = path.name
    try:
        if name == "package.json":
            data = json.loads(text)
            deps: dict[str, str] = {}
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                deps.update(data.get(key) or {})
            # Local/remote refs aren't registry packages.
            deps = {k: v for k, v in deps.items()
                    if isinstance(v, str) and not re.match(r"(file:|link:|git|https?:|workspace:|\.)", v)}
            return "npm", deps

        if name == "pyproject.toml":
            data = tomllib.loads(text)
            raw = list((data.get("project") or {}).get("dependencies") or [])
            for group in ((data.get("dependency-groups") or {}).values()):
                raw += [d for d in group if isinstance(d, str)]
            deps = {}
            for spec in raw:
                m = re.match(r"^\s*([A-Za-z0-9][\w.\-]*)\s*(?:\[[^\]]*\])?\s*([<>=!~^ ]*[\w.\-]*)", spec)
                if m and not re.search(r"@|file:|git\+", spec):
                    deps[m.group(1)] = (m.group(2) or "").strip()
            return "pypi", deps

        if name in ("requirements.txt", "requirements-dev.txt"):
            deps = {}
            for line in text.splitlines():
                line = line.split("#")[0].strip()
                if not line or line.startswith("-") or re.search(r"@|file:|git\+", line):
                    continue
                m = re.match(r"^([A-Za-z0-9][\w.\-]*)\s*(?:\[[^\]]*\])?\s*([<>=!~]*[\w.\-]*)", line)
                if m:
                    deps[m.group(1)] = (m.group(2) or "").strip()
            return "pypi", deps
    except Exception:
        return "", {}
    return "", {}


_INSTALL_CMDS = [
    (r"(?:npm (?:install|i)|yarn add|pnpm add)\s+([^\n`|;&]+)", "npm"),
    (r"(?:pip install|uv add|poetry add)\s+([^\n`|;&]+)", "pypi"),
]

# "react-native": "0.72.6" — a version, not a script. "start": "nx serve" won't match.
_JSON_DEP = re.compile(r'"([@\w/.\-]+)"\s*:\s*"([\^~>=<]*\d[\w.\-]*)"')

# Bullets under a Dependencies heading: - name@1.2.3 / - name (1.2.3) / - name
# The name must not swallow the @version, so `@` is allowed only as a scope prefix.
_BULLET = re.compile(
    r"^\s*[-*]\s*`?(@?[\w.\-]+(?:/[\w.\-]+)?)`?\s*(?:[@(]\s*v?([\^~>=<]*\d[\w.\-]*)\)?)?",
    re.M,
)


# One parser for every way a package can be pinned:
#   rich==13.7.0 · react-native@0.72.6 · @scope/pkg · requests>=2.0 · pkg
_REQUIREMENT = re.compile(r"^(@?[\w.\-]+(?:/[\w.\-]+)?)(?:[@=<>!~]+\s*v?([\^~>=<]*\d[\w.\-]*))?$")


def _split_requirement(token: str) -> tuple[str, str]:
    m = _REQUIREMENT.match(token.strip().strip("`,\"'"))
    return (m.group(1), m.group(2) or "") if m else ("", "")


def _guess_ecosystem(text: str) -> str:
    """A bare bullet like `- requests` carries no registry. Infer it from what the
    spec is plainly about — install commands and JSON blocks say so themselves.
    No clear signal → "auto", which accepts the package if either registry has it."""
    lowered = text.lower()
    py = sum(lowered.count(w) for w in ("pyproject", "pip install", "python", "requirements.txt"))
    js = sum(lowered.count(w) for w in ("package.json", "npm", "react", "node", "typescript"))
    if py == js:
        return "auto"
    return "pypi" if py > js else "npm"


def extract_packages(text: str) -> list[tuple[str, str, str]]:
    """(ecosystem, name, version) for every package a spec names — in install
    commands, JSON dependency blocks, or a Dependencies section."""
    found: dict[tuple[str, str], str] = {}

    def add(eco: str, name: str, version: str = "") -> None:
        name = name.strip().strip("`,\"'")
        if not name or name.startswith("-") or "/" in name and not name.startswith("@"):
            return
        key = (eco, name)
        if version or key not in found:
            found[key] = version

    for pattern, eco in _INSTALL_CMDS:
        for args in re.findall(pattern, text, re.I):
            for token in args.split():
                if token.startswith("-"):  # flags: -D, --save-dev
                    continue
                name, version = _split_requirement(token)
                if name:
                    add(eco, name, version)

    for name, version in _JSON_DEP.findall(text):
        add("npm", name, version)

    section = re.search(r"^#+\s*Dependencies\s*$(.*?)(?=^#+\s|\Z)", text, re.M | re.S)
    if section:
        eco = _guess_ecosystem(text)
        for name, version in _BULLET.findall(section.group(1)):
            if name.lower() not in ("none", "n/a", "confirmed", "verified"):
                add(eco, name, version)

    return [(eco, name, ver) for (eco, name), ver in found.items()]


def check_spec(text: str) -> list[str]:
    """Problems with every package a spec names. Empty = all real."""
    packages = extract_packages(text)
    if not packages:
        return []
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda p: check_one(*p), packages)
    return [r for r in results if r]


def check_manifest(path: Path, text: str) -> list[str]:
    """Problems with the dependencies declared in a manifest. Empty = all real."""
    ecosystem, deps = _manifest_deps(path, text)
    if not deps:
        return []
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda kv: check_one(ecosystem, kv[0], kv[1]), deps.items())
    return [r for r in results if r]
