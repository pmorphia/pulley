"""Packages pulled apart from any project.

``pulley pull author.name`` installs a package for this user, and ``--system`` for the
Flywheel installation. Each install is its own environment, ``<installs>/<name>/``, holding a
pulley.toml that names the one package, its pulley.lock, and a ``.flywheel-modules`` with the
package and its dependencies. Installs never share dependencies with each other or with a
project. No project's pulley.toml is read or changed.

An install provides its package as a library, its ``[commands]``, or both:

- The library is ``<flywheel>/modules/<name>.link``, which points into the environment. The
  compiler searches that directory after a program's own ``.flywheel-modules``, so any program
  can ``import <name>``, and the library finds its own dependencies in its environment.
- Each command is a launcher in a bin directory, a small script that runs the command's
  program with the environment's packages.

A project's direct dependencies get launchers too, in ``.flywheel-modules/.bin``, which
``pulley run`` uses.

| scope   | environments                       | launchers                     |
|---------|------------------------------------|-------------------------------|
| user    | ``<data>/installs`` (~/.local/share/pulley) | ``~/.local/bin`` (``bin_dir``) |
| system  | ``<flywheel>/pulley/installs``     | ``<flywheel>/bin``, next to flywheel |

Libraries are installation-wide for both scopes: Flywheel has one ``modules`` directory.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import config as cfg
from . import install as installer
from . import manifest as manifests
from .config import LOCAL_MODULES, LOCKFILE, MANIFEST, PulleyError

WINDOWS = os.name == "nt"
RECORD = ".pulley-install.json"
MARK = "pulley-launcher:"
PROJECT_BIN = ".bin"


@dataclass
class Scope:
    name: str  # "user" or "system"
    installs: Path
    bin: Path
    flywheel: Path | None

    def describe(self) -> str:
        return "this user" if self.name == "user" else f"the Flywheel installation ({self.installs.parent.parent})"


def scope(system: bool) -> Scope:
    if system:
        root = cfg.flywheel_root()
        own = root / "bin" / ("flywheel.exe" if WINDOWS else "flywheel")
        return Scope("system", root / "pulley" / "installs", root / "bin", own if own.is_file() else cfg.find_flywheel())
    return Scope("user", cfg.data_dir() / "installs", cfg.user_bin_dir(), cfg.find_flywheel())


# Launchers -------------------------------------------------------------------------------

def launcher_path(directory: Path, command: str) -> Path:
    return directory / (command + ".cmd" if WINDOWS else command)


def _launcher(flywheel: Path, program: Path, owner: str, note: str) -> str:
    if WINDOWS:
        return (f"@echo off\r\nrem {MARK} {owner}\r\nrem {note}\r\n"
                f'"{flywheel}" "{program}" -- %*\r\n')
    return (f"#!/bin/sh\n# {MARK} {owner}\n# {note}\n"
            f"exec {shlex.quote(str(flywheel))} {shlex.quote(str(program))} -- \"$@\"\n")


def launcher_owner(path: Path) -> str | None:
    """Who a launcher was installed for, or None when the file is not a Pulley launcher."""
    try:
        with path.open(encoding="utf-8", errors="replace") as file:
            head = [file.readline() for _ in range(3)]
    except OSError:
        return None
    for line in head:
        at = line.find(MARK)
        if at >= 0:
            return line[at + len(MARK):].strip()
    return None


def _write_launcher(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.pulley")
    temporary.write_text(text, encoding="utf-8", newline="")
    if not WINDOWS:
        temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(temporary, path)


def package_directory(modules: Path, alias: str) -> Path | None:
    """Where an installed package's files are: its directory, or the target of its .link."""
    if (modules / alias).is_dir():
        return modules / alias
    link = modules / f"{alias}.link"
    if link.is_file():
        target = Path(link.read_text(encoding="utf-8").strip())
        return target if target.is_dir() else None
    return None


def package_commands(directory: Path) -> dict[str, Path]:
    """An installed package's commands and the programs they run."""
    if not (directory / MANIFEST).is_file():
        return {}
    manifest = manifests.load(directory)
    out = {}
    for name, program in manifest.commands.items():
        path = directory / program
        if not path.is_file():
            raise PulleyError(f"{manifest.author}.{manifest.name}: command '{name}' runs {program}, which it doesn't contain")
        out[name] = path
    return out


def need_flywheel(flywheel: Path | None) -> Path:
    if not flywheel:
        raise PulleyError("commands run with flywheel, which isn't on PATH; install Flywheel or run "
                          "`pulley config flywheel /path/to/flywheel`")
    return flywheel.resolve()


def project_launchers(modules: Path, aliases: list[str], log=print) -> int:
    """Rewrites a project's .flywheel-modules/.bin from its direct dependencies' commands."""
    directory = modules / PROJECT_BIN
    if directory.is_dir():
        shutil.rmtree(directory)
    wanted: dict[str, tuple[str, Path]] = {}
    for alias in aliases:
        where = package_directory(modules, alias)
        for name, program in (package_commands(where) if where else {}).items():
            if name in wanted and wanted[name][0] != alias:
                log(f"command {name} is in both {wanted[name][0]} and {alias}; using {wanted[name][0]}")
                continue
            wanted[name] = (alias, program)
    if not wanted:
        return 0
    flywheel = cfg.find_flywheel()
    if not flywheel:
        log(f"flywheel isn't on PATH, so the commands of the dependencies ({', '.join(sorted(wanted))}) weren't set up")
        return 0
    flywheel = flywheel.resolve()
    for name, (alias, program) in sorted(wanted.items()):
        _write_launcher(launcher_path(directory, name),
                        _launcher(flywheel, program, "project", f"From the dependency {alias}; `pulley pull` rewrites it."))
    return len(wanted)


def project_command(modules: Path, name: str) -> Path | None:
    path = launcher_path(modules / PROJECT_BIN, name)
    return path if path.is_file() else None


# Installs --------------------------------------------------------------------------------

def records(where: Scope) -> dict[str, dict]:
    """The installs of a scope: name -> record."""
    out = {}
    if where.installs.is_dir():
        for env in sorted(where.installs.iterdir()):
            if env.name.startswith(".") or not (env / RECORD).is_file():
                continue
            try:
                out[env.name] = json.loads((env / RECORD).read_text(encoding="utf-8"))
            except ValueError:
                out[env.name] = {}
    return out


def find(where: Scope, name: str) -> str | None:
    """An install named by its name, its distribution (author.name), or one of its commands."""
    installed = records(where)
    if name in installed:
        return name
    for env, record in installed.items():
        if name == record.get("distribution") or name in record.get("commands", {}):
            return env
    return None


def _environment_manifest(dependency: manifests.Dependency) -> str:
    return ("# Made by `pulley pull`; `pulley uninstall " + dependency.alias + "` removes it.\n\n"
            f"[dependencies]\n{dependency.alias} = {dependency.toml()}\n")


def library_link(name: str) -> Path:
    """Where an installed library is linked: the Flywheel installation's modules directory."""
    return cfg.flywheel_root() / "modules" / f"{name}.link"


def _link_target(link: Path) -> Path | None:
    try:
        text = link.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text) if text else None


def owns_library(where: Scope, name: str, link: Path) -> bool:
    """Whether a library link points into this scope's install `name`."""
    target = _link_target(link)
    if target is None:
        return False
    try:
        return target.resolve().is_relative_to((where.installs / name).resolve())
    except OSError:
        return False


def install(where: Scope, dependency: manifests.Dependency, resolve, choose, force: bool = False,
            log=print) -> dict:
    """Installs one package into its own environment, as a library and/or its commands.

    `resolve(directory)` resolves and installs the pulley.toml in `directory` and returns the
    resolved packages (the same as installing a project). `choose(label, commands, previous)`
    gets the package's label, its command names, and the previous install's record (or None),
    and returns (library, commands): what to install. Returns the install's record.
    """
    name = dependency.alias
    env = where.installs / name
    previous = records(where).get(name)
    staging = where.installs / f".{name}.installing"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        (staging / MANIFEST).write_text(_environment_manifest(dependency), encoding="utf-8")
        packages = resolve(staging)
        root = next(p for p in packages.values() if name in p.aliases)
        label = root.label
        directory = package_directory(staging / LOCAL_MODULES, name)
        available = package_commands(directory) if directory else {}
        want_library, want_commands = choose(label, sorted(available), previous)
        commands = available if want_commands else {}
        flywheel = need_flywheel(where.flywheel) if commands else None
        link = library_link(name) if want_library else None
        if link is not None:
            if (link.parent / name).exists():
                raise PulleyError(f"{link.parent / name} already provides `import {name}`; remove it, "
                                  f"or pull with --as to use another name")
            if link.exists() and not owns_library(where, name, link) and not force:
                raise PulleyError(f"{link} already provides `import {name}` (from {_link_target(link)}); "
                                  f"pull with --force to replace it, or --as to use another name")
        # Launchers that are in the way: another install's (only with --force) or not Pulley's.
        taken: dict[str, str] = {}
        for command in commands:
            path = launcher_path(where.bin, command)
            if not path.exists():
                continue
            owner = launcher_owner(path)
            if owner is None:
                raise PulleyError(f"{path} exists and wasn't installed by Pulley; remove it to install the {command} command")
            if owner != name:
                if not force:
                    raise PulleyError(f"the {command} command is already installed by {owner}; "
                                      f"`pulley uninstall {owner}` first, or pull with --force to take it over")
                taken[command] = owner
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # Swap the new environment in, then point the library and launchers at it.
    if env.exists():
        old = where.installs / f".{name}.old"
        if old.exists():
            shutil.rmtree(old)
        env.rename(old)
        staging.rename(env)
        shutil.rmtree(old, ignore_errors=True)
    else:
        staging.rename(env)
    final = package_directory(env / LOCAL_MODULES, name)
    library = ""
    if link is not None:
        link.parent.mkdir(parents=True, exist_ok=True)
        temporary = link.with_name(f".{link.name}.pulley")
        temporary.write_text(str(final.resolve()) + "\n", encoding="utf-8")
        os.replace(temporary, link)
        library = str(link)
    elif (previous or {}).get("library"):
        old_link = Path(previous["library"])
        if owns_library(where, name, old_link):
            old_link.unlink(missing_ok=True)
    note = f"Installed by Pulley for {where.name} install {name}; `pulley uninstall {name}" + \
           (" --system" if where.name == "system" else "") + "` removes it."
    launchers = {}
    for command in sorted(commands):
        path = launcher_path(where.bin, command)
        program = final / commands[command].relative_to(directory)
        _write_launcher(path, _launcher(flywheel, program, name, note))
        launchers[command] = str(path)
    for command, owner in taken.items():
        _drop_command(where, owner, command)
        log(f"took the {command} command over from {owner}")
    for command, path in (previous or {}).get("commands", {}).items():
        if command not in launchers and launcher_owner(Path(path)) == name:
            Path(path).unlink(missing_ok=True)
    record = {
        "name": name,
        "distribution": f"{root.author}.{root.name}" if root.kind == "registry" else "",
        "package": label,
        "source": dependency.describe(),
        "library": library,
        "commands": launchers,
        "available": sorted(available),
        "installed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (env / RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def _drop_command(where: Scope, owner: str, command: str) -> None:
    path = where.installs / owner / RECORD
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    record.get("commands", {}).pop(command, None)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def uninstall(where: Scope, name: str) -> dict:
    record = records(where).get(name, {})
    if record.get("library") and owns_library(where, name, Path(record["library"])):
        Path(record["library"]).unlink(missing_ok=True)
    for command, path in record.get("commands", {}).items():
        if launcher_owner(Path(path)) == name:
            Path(path).unlink(missing_ok=True)
    shutil.rmtree(where.installs / name)
    return record


def environment_dependency(where: Scope, name: str) -> manifests.Dependency:
    """The dependency an install was made from (to install it again, newer)."""
    manifest = manifests.load(where.installs / name)
    if name not in manifest.dependencies:
        raise PulleyError(f"{where.installs / name / MANIFEST} doesn't name {name}; reinstall it")
    return manifest.dependencies[name]


def installed_version(where: Scope, name: str) -> str:
    for entry in installer.read_lock(where.installs / name).values():
        if name in entry.get("aliases", []):
            return entry.get("version") or entry.get("commit", "")[:12] or entry.get("path", "")
    return ""


def on_path(directory: Path) -> bool:
    entries = [Path(p).expanduser() for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    try:
        target = directory.resolve()
    except OSError:
        return False
    return any(e.resolve() == target for e in entries if e.exists())


def shadowed(command: str, launcher: Path) -> str | None:
    """Another program that runs instead of a launcher, when it comes first on PATH."""
    found = shutil.which(command)
    if found and Path(found).resolve() != launcher.resolve():
        return found
    return None


def run_command(launcher: Path, arguments: list[str], cwd: Path) -> int:
    command = ["cmd", "/c", str(launcher), *arguments] if WINDOWS else [str(launcher), *arguments]
    return subprocess.call(command, cwd=cwd)
