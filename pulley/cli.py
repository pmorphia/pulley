"""The `pulley` (also `ppm`) command."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from . import config as cfg
from . import install as installer
from . import manifest as manifests
from . import package as packaging
from . import tools
from .config import LOCAL_MODULES, MANIFEST, PulleyError
from .registry import Registry
from .resolver import Resolver
from .versions import Constraint, Version, best, caret

# Output ----------------------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
QUIET = False


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def say(message: str) -> None:
    if not QUIET:
        print(message)


def step(message: str) -> None:
    if not QUIET:
        print(_paint("  " + message, "90"))


def done(message: str) -> None:
    print(_paint("✓ ", "32") + message if _COLOR else message)


def warn(message: str) -> None:
    print(_paint("warning: ", "33") + message, file=sys.stderr)


# Targets: a project's dependencies (.flywheel-modules) ---------------------------------------

class Target:
    """A project: its pulley.toml and its .flywheel-modules."""

    def __init__(self):
        project = cfg.find_project()
        if project is None:
            raise PulleyError(f"no {MANIFEST} here or in a parent directory; run `pulley init` "
                              "(`pulley install author.name` installs a package's commands instead)")
        self.directory = project
        self.modules = project / LOCAL_MODULES

    def manifest(self) -> manifests.Manifest:
        return manifests.load(self.directory)

    def describe(self) -> str:
        return str(self.directory)


def _registry_name(args, manifest: manifests.Manifest | None = None) -> str:
    return getattr(args, "registry", None) or cfg.default_registry(manifest.registry if manifest else None)


def resolve_into(directory: Path, args, update: set[str] | None = None, update_all: bool = False,
                 production: bool = False, copy_paths: bool = False, frozen: bool = False):
    """Resolves the pulley.toml in `directory`, writes its lock, and fills its modules directory."""
    manifest = manifests.load(directory)
    registry = _registry_name(args, manifest)
    lock = installer.read_lock(directory)
    resolver = Resolver(manifest, registry, lock, update=update, update_all=update_all,
                        include_dev=not production, log=step if getattr(args, "verbose", False) else (lambda m: None))
    packages = resolver.resolve()
    if frozen:
        def state(entry_version, entry_commit, aliases):
            return (entry_version or None, entry_commit or None, tuple(sorted(aliases)))
        locked = {k: state(e.get("version"), e.get("commit"), e.get("aliases", [])) for k, e in lock.items()}
        resolved = {k: state(p.version, p.commit, p.aliases) for k, p in packages.items()}
        if locked != resolved:
            raise PulleyError(f"{MANIFEST} and {cfg.LOCKFILE} disagree (run `pulley install` without --frozen)")
    else:
        installer.write_lock(directory, packages)
    counts = installer.sync(directory / LOCAL_MODULES, packages, include_dev=not production, log=step,
                            copy_paths=copy_paths)
    return manifest, packages, counts


def install_target(target: Target, args, update: set[str] | None = None, update_all: bool = False) -> None:
    production = getattr(args, "production", False)
    manifest, packages, counts = resolve_into(target.directory, args, update, update_all, production=production,
                                              frozen=getattr(args, "frozen", False))
    direct = [d.alias for d in manifest.all_dependencies(dev=not production)]
    commands = tools.project_launchers(target.modules, direct, log=warn)
    total = sum(1 for p in packages.values() if not (p.dev and production))
    changes = ", ".join(f"{n} {kind}" for kind, n in counts.items() if n and kind != "unchanged")
    done(f"{total} package(s) installed in {target.modules}" + (f" ({changes})" if changes else "")
         + (f"; {commands} command(s) for `pulley run`" if commands else ""))


# Dependency specs given on the command line -------------------------------------------------

def _alias_for(name: str) -> str:
    alias = name.replace("-", "_")
    return alias if manifests.ALIAS.match(alias) else "_" + alias


def _parse_spec(text: str, args, manifest: manifests.Manifest) -> manifests.Dependency:
    alias = getattr(args, "alias", None)
    named = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.+)$", text)
    if named:  # alias=author.name...
        alias, text = named.group(1), named.group(2)
    author, name, registry, constraint = manifests.parse_address(text)
    registry = registry or getattr(args, "registry", None)
    if registry == cfg.default_registry(manifest.registry):
        registry = None
    dependency = manifests.Dependency(alias or _alias_for(name), "registry", author, name, registry, constraint,
                                      dev=getattr(args, "dev", False))
    if constraint in ("*", "latest"):
        # Record a compatible-release constraint on the newest version, as `npm install` does.
        r = Registry(registry or cfg.default_registry(manifest.registry))
        versions = r.versions(author, name)
        newest = best(versions, [Constraint("*")]) or r.latest(author, name)
        if not newest:
            raise PulleyError(f"{author}.{name} has no releases on {r}")
        dependency.constraint = caret(newest)
    return dependency


def _path_dependency(path: str, args, base: Path) -> manifests.Dependency:
    directory = (Path.cwd() / path).resolve()
    if not directory.is_dir():
        raise PulleyError(f"{path} is not a directory")
    name = directory.name
    if (directory / MANIFEST).is_file():
        name = manifests.load(directory).name or name
    relative = os.path.relpath(directory, base)
    return manifests.Dependency(getattr(args, "alias", None) or _alias_for(name), "path", path=relative,
                                dev=getattr(args, "dev", False))


def _add(target: Target, args) -> list[manifests.Dependency]:
    manifest = target.manifest()
    deps = []
    if args.path:
        deps.append(_path_dependency(args.path, args, target.directory))
    if args.git:
        name = args.git.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        deps.append(manifests.Dependency(args.alias or _alias_for(name.lower()), "git", git=args.git,
                                         rev=args.rev or "", dev=args.dev))
    for spec in args.packages:
        deps.append(_parse_spec(spec, args, manifest))
    if args.alias and len(deps) > 1:
        raise PulleyError("--as names one package")
    if not deps:
        raise PulleyError("name a package (author.name), --path, or --git")
    for dep in deps:
        table = "dev-dependencies" if dep.dev else "dependencies"
        other = "dependencies" if dep.dev else "dev-dependencies"
        manifests.remove_value(manifest.path, other, dep.alias)
        manifests.set_value(manifest.path, table, dep.alias, dep.toml())
        say(f"{'+' if dep.alias not in manifest.dependencies and dep.alias not in manifest.dev_dependencies else '~'} "
            f"{dep.alias} = {dep.describe()}" + (" (dev)" if dep.dev else ""))
    return deps


# Commands --------------------------------------------------------------------------------

def cmd_init(args) -> None:
    directory = Path(args.directory or ".").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / MANIFEST).exists():
        raise PulleyError(f"{directory / MANIFEST} already exists")
    name = (args.name or directory.name).lower().replace(" ", "-")
    if not manifests.NAME.match(name):
        raise PulleyError(f"'{name}' is not a valid package name; pass --name")
    author = (args.author or os.environ.get("USER") or os.environ.get("USERNAME") or "me").lower()
    main = args.main or f"{name.replace('-', '_')}.fw"
    (directory / MANIFEST).write_text(manifests.template(name, author, main), encoding="utf-8")
    created = [MANIFEST]
    if getattr(args, "template", None) == "game":
        created += _game_template(directory, name, main)
    if not (directory / main).exists():
        (directory / main).write_text(
            f"# {name}\n\nfunction greeting(string who) returns string\n    return \"Hello, {{who}}!\"\nend\n",
            encoding="utf-8")
        created.append(main)
    tests = directory / "tests"
    if args.tests and not tests.exists():
        tests.mkdir()
        module = main.removesuffix(".fw")
        (tests / f"test_{module}.fw").write_text(
            "import testing\n\nfunction test_truth\n    testing.assert(true)\nend\n", encoding="utf-8")
        created.append(f"tests/test_{module}.fw")
    ignore = directory / ".gitignore"
    lines = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
    if f"{LOCAL_MODULES}/" not in lines:
        ignore.write_text("\n".join(lines + [f"{LOCAL_MODULES}/"]) + "\n", encoding="utf-8")
        created.append(".gitignore")
    done(f"created {name} in {directory} ({', '.join(created)})")
    if getattr(args, "template", None) == "game":
        say(f"next: cd {args.directory or '.'} && pulley install && pulley run start")


GAME_TEMPLATE = """# {name}: a game made with Aperture.
#
#     pulley run start        (or: flywheel {main})
#     flywheel --build {main} -O3 -o {name}
import aperture as ap
import math

ap.open("{name}", 1280, 720, ap.RESIZABLE)
ap.virtual_size(640, 360)          # draw in 640x360, scaled to the window

var player = ap.center()
var score = 0
var coin = ap.random_in(ap.Rect(40, 40, 560, 280))
var chime = ap.tone(ap.WAVE_TRIANGLE, 660, 0.15, 1320, 0.4)
var sparks = ap.Particles()

function update(double dt)
    player = player + ap.steer() * (220 * dt)
    player = ap.Vec2(math.clamp(player.x, 12.0, 628.0), math.clamp(player.y, 12.0, 348.0))
    if ap.circles_overlap(player, 12, coin, 8) do
        score += 1
        chime.play(0.5)
        sparks.burst(coin, 20)
        coin = ap.random_in(ap.Rect(40, 40, 560, 280))
    end
    sparks.update()
end

function draw()
    ap.clear(ap.NAVY)
    ap.circle(coin.x, coin.y, 8 + 2 * ap.pulse(0.6), ap.YELLOW)
    sparks.draw()
    ap.circle(player.x, player.y, 12, ap.ORANGE)
    ap.text("SCORE {{score}}", 12, 10, 16)
    ap.text("Arrow keys, WASD, or a gamepad", 12, 340, 8, ap.GRAY)
end

while ap.frame() do
    update(ap.delta())
    draw()
    if ap.key_pressed(ap.KEY_ESCAPE) do
        ap.quit()
    end
end
ap.close()
"""


def _game_template(directory: Path, name: str, main: str) -> list[str]:
    """A project that opens a window and plays: Aperture as a dependency, a game loop, assets/."""
    manifest = directory / MANIFEST
    manifests.set_value(manifest, "dependencies", "aperture", '"polymorphia.aperture=^1.0.0"')
    manifests.set_value(manifest, "scripts", "start", f'"flywheel {main}"')
    created = []
    if not (directory / main).exists():
        (directory / main).write_text(GAME_TEMPLATE.format(name=name, main=main), encoding="utf-8")
        created.append(main)
    assets = directory / "assets"
    if not assets.exists():
        assets.mkdir()
        (assets / "README.md").write_text(
            "Images, sounds, and fonts for the game. Load them relative to the project:\n\n"
            '    var ship = ap.load_texture("assets/ship.png")\n', encoding="utf-8")
        created.append("assets/")
    return created


def cmd_new(args) -> None:
    if Path(args.directory).exists():
        raise PulleyError(f"{args.directory} already exists")
    args.tests = True
    cmd_init(args)


def cmd_add(args) -> None:
    target = Target()
    path = target.directory / MANIFEST
    original = path.read_text(encoding="utf-8")
    try:
        _add(target, args)
        if not args.no_install:
            install_target(target, args)
    except BaseException:
        # A dependency that cannot be resolved or installed is not recorded.
        path.write_text(original, encoding="utf-8")
        raise


def cmd_install(args) -> None:
    if args.packages or args.path or args.git:
        _install_software(args)
        return
    if args.system or args.force:
        raise PulleyError("name the package to install: pulley install author.name")
    install_target(Target(), args)


def _scope(args) -> tools.Scope:
    return tools.scope(getattr(args, "system", False))


def _software_dependencies(args) -> list[manifests.Dependency]:
    deps = []
    if args.path:
        directory = (Path.cwd() / args.path).resolve()
        if not directory.is_dir():
            raise PulleyError(f"{args.path} is not a directory")
        name = manifests.load(directory).name if (directory / MANIFEST).is_file() else ""
        deps.append(manifests.Dependency(args.alias or _alias_for(name or directory.name.lower()), "path",
                                         path=str(directory)))
    if args.git:
        name = args.git.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        deps.append(manifests.Dependency(args.alias or _alias_for(name.lower()), "git", git=args.git, rev=args.rev or ""))
    for spec in args.packages:
        deps.append(_parse_spec(spec, args, manifests.Manifest(directory=Path.cwd())))
    if args.alias and len(deps) > 1:
        raise PulleyError("--as names one package")
    return deps


def _install_software(args, deps: list[manifests.Dependency] | None = None, verb: str = "installed") -> None:
    if getattr(args, "dev", False) or getattr(args, "no_install", False) or getattr(args, "frozen", False):
        raise PulleyError("-D, --no-install and --frozen are for a project's dependencies (`pulley add`)")
    where = _scope(args)
    resolve = lambda directory: resolve_into(directory, args, update_all=True, production=True, copy_paths=True)[1]
    for dep in deps if deps is not None else _software_dependencies(args):
        record = tools.install(where, dep, resolve, force=getattr(args, "force", False), log=step)
        names = ", ".join(record["commands"])
        done(f"{verb} {record['package']} for {where.describe()}: {names}")
        for command, path in record["commands"].items():
            other = tools.shadowed(command, Path(path))
            if other:
                warn(f"`{command}` runs {other}, which comes before {where.bin} on PATH")
        if not tools.on_path(where.bin):
            warn(f"{where.bin} isn't on PATH; add it to run {names} by name")


def cmd_uninstall(args) -> None:
    where = _scope(args)
    for name in args.names:
        found = tools.find(where, name)
        if not found:
            if cfg.find_project() and not args.system:
                raise PulleyError(f"{name} isn't installed for {where.describe()} (`pulley remove {name}` removes a dependency)")
            raise PulleyError(f"{name} isn't installed for {where.describe()}")
        record = tools.uninstall(where, found)
        done(f"uninstalled {record.get('package', found)}" + (f" ({', '.join(record.get('commands', {}))})" if record.get("commands") else ""))


def cmd_remove(args) -> None:
    target = Target()
    manifest = target.manifest()
    original = manifest.path.read_text(encoding="utf-8")
    try:
        _remove_all(target, manifest, args)
    except BaseException:
        manifest.path.write_text(original, encoding="utf-8")
        raise


def _remove_all(target: Target, manifest: manifests.Manifest, args) -> None:
    for alias in args.names:
        if not any(manifests.remove_value(manifest.path, t, alias) for t in ("dependencies", "dev-dependencies")):
            raise PulleyError(f"'{alias}' is not a dependency of {target.describe()}")
        say(f"- {alias}")
    install_target(target, args)


def cmd_update(args) -> None:
    if args.user or args.system:
        where = _scope(args)
        names = [tools.find(where, n) or n for n in args.names] or list(tools.records(where))
        if not names:
            say(f"nothing is installed for {where.describe()}")
            return
        for name in names:
            if name not in tools.records(where):
                raise PulleyError(f"{name} isn't installed for {where.describe()}")
            before = tools.installed_version(where, name)
            _install_software(args, [tools.environment_dependency(where, name)], verb="updated")
            after = tools.installed_version(where, name)
            if before != after:
                say(f"  {name}: {before} -> {after}")
        return
    target = Target()
    names = set(args.names)
    manifest = target.manifest()
    unknown = names - {d.alias for d in manifest.all_dependencies()} - {
        a for e in installer.read_lock(target.directory).values() for a in e.get("aliases", [])}
    if unknown:
        raise PulleyError(f"not installed: {', '.join(sorted(unknown))}")
    before = installer.read_lock(target.directory)
    install_target(target, args, update=names, update_all=not names)
    after = installer.read_lock(target.directory)
    changed = False
    for key, entry in after.items():
        old = before.get(key, {})
        if old.get("version") != entry.get("version") or old.get("commit") != entry.get("commit"):
            was = old.get("version") or (old.get("commit") or "")[:12] or "new"
            now = entry.get("version") or entry.get("commit", "")[:12]
            say(f"  {', '.join(entry['aliases'])}: {was} -> {now}")
            changed = True
    if not changed:
        say("everything is at the newest version its constraints allow")


def _list_installs(scopes: list[bool]) -> None:
    for system in scopes:
        try:
            where = tools.scope(system)
        except PulleyError as exc:
            say(f"system: {exc}")
            continue
        installed = tools.records(where)
        say(f"installed for {where.describe()} (commands in {where.bin})")
        if not installed:
            say("  (nothing)")
        for name, record in installed.items():
            version = tools.installed_version(where, name)
            source = record.get("distribution") or record.get("source", "")
            commands = ", ".join(record.get("commands", {})) or "no commands"
            say(f"  {name} {source}" + (f"={version}" if version and record.get("distribution") else "") + f": {commands}")


def cmd_list(args) -> None:
    if args.user or args.system or not cfg.find_project():
        _list_installs([False, True] if not (args.user or args.system) else [s for s, on in ((False, args.user), (True, args.system)) if on])
        return
    target = Target()
    manifest = target.manifest()
    lock = installer.read_lock(target.directory)
    present = installer.installed(target.modules)
    by_alias = {a: e for e in lock.values() for a in e.get("aliases", [])}
    say(f"{manifest.name or 'this project'}" + (f" {manifest.version}" if manifest.name else "") + f" ({target.describe()})")

    def describe(alias: str) -> str:
        entry = by_alias.get(alias)
        if not entry:
            return f"{alias} (not installed; run `pulley install`)"
        if entry["source"] == "registry":
            text = f"{alias} {entry['distribution']}={entry['version']}"
            if entry["registry"] != cfg.default_registry(manifest.registry):
                text += f" @{entry['registry']}"
        elif entry["source"] == "path":
            text = f"{alias} -> {entry['path']}"
        else:
            text = f"{alias} git {entry['git']} {entry.get('commit', '')[:12]}"
        if alias not in present:
            text += " (missing from the modules directory; run `pulley install`)"
        return text

    def tree(alias: str, prefix: str, seen: set[str]) -> None:
        entry = by_alias.get(alias, {})
        children = entry.get("dependencies", []) if args.tree else []
        for i, child in enumerate(children):
            last = i == len(children) - 1
            say(prefix + ("└── " if last else "├── ") + describe(child) + (" (see above)" if child in seen else ""))
            if child not in seen:
                seen.add(child)
                tree(child, prefix + ("    " if last else "│   "), seen)

    roots = manifest.all_dependencies()
    if not roots:
        say("  (no dependencies)")
    seen: set[str] = set()
    for dep in roots:
        say("  " + describe(dep.alias) + (" (dev)" if dep.dev else ""))
        seen.add(dep.alias)
        tree(dep.alias, "    ", seen)
    if not args.tree:
        others = sorted(set(by_alias) - {d.alias for d in roots})
        if others:
            say(f"  + {len(others)} indirect: {', '.join(others)} (--tree shows where they come from)")


def cmd_outdated(args) -> None:
    target = Target()
    manifest = target.manifest()
    lock = installer.read_lock(target.directory)
    requirements = {d.alias: d for d in manifest.all_dependencies()}
    rows = []
    for key, entry in sorted(lock.items(), key=lambda kv: kv[1]["aliases"]):
        if entry["source"] != "registry":
            continue
        _, registry, author, name = key
        versions = Registry(registry).versions(author, name)
        newest = best(versions, [Constraint("*")]) or entry["version"]
        direct = [requirements[a] for a in entry["aliases"] if a in requirements]
        wanted = best(versions, [Constraint(d.constraint) for d in direct]) if direct else entry["version"]
        if Version.parse(newest) != Version.parse(entry["version"]) or (wanted and wanted != entry["version"]):
            rows.append((", ".join(entry["aliases"]), f"{author}.{name}", entry["version"], wanted or "-", newest))
    if not rows:
        done("everything is up to date")
        return
    header = ("package", "distribution", "installed", "wanted", "latest")
    widths = [max(len(str(r[i])) for r in rows + [header]) for i in range(5)]
    for row in [header] + rows:
        say("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))
    say("`pulley update` moves to the wanted versions; change the constraint in pulley.toml for the latest.")


def cmd_info(args) -> None:
    author, name, registry, constraint = manifests.parse_address(args.address)
    manifest = manifests.load(cfg.find_project()) if cfg.find_project() else None
    r = Registry(registry or _registry_name(args, manifest))
    versions = sorted(r.versions(author, name), key=Version.parse)
    chosen = best(versions, [Constraint(constraint)]) if constraint not in ("*", "latest") else (r.latest(author, name) or (versions[-1] if versions else None))
    if not chosen:
        raise PulleyError(f"no release of {author}.{name} matches {constraint}")
    for line in r.view(author, name, chosen):
        say(line)
    say(f"Versions: {', '.join(versions)}")
    where = f"@{r}" if str(r) != cfg.default_registry() else ""
    say(f"Add it to a project: pulley add {author}.{name}{where}")
    say(f"Install its commands, if it has any: pulley install {author}.{name}{where}")


def cmd_publish(args) -> None:
    project = cfg.find_project()
    if not project:
        raise PulleyError(f"no {MANIFEST} here or in a parent directory")
    manifest = manifests.load(project)
    packaging.check(manifest)
    registry = Registry(_registry_name(args, manifest))
    address = registry.address(manifest.author, manifest.name, manifest.version)
    with tempfile.TemporaryDirectory(prefix="pulley-publish-") as temporary:
        staged = Path(temporary) / manifest.name
        files = packaging.stage(manifest, staged)
        size = sum((staged / f).stat().st_size for f in files)
        say(f"{address}: {len(files)} file(s), {size} bytes")
        if args.verbose or args.dry_run:
            for f in files:
                step(f.as_posix())
        if args.dry_run:
            archive = Path(temporary) / "release.tar.gz"
            packaging.pack(manifest, archive)
            done(f"dry run: the archive would be {archive.stat().st_size} bytes; nothing was published")
            return
        username, password = (None, None) if registry.local else cfg.credentials(registry.spec)
        if not registry.local and not (username and password):
            raise PulleyError(f"not logged in to {registry}; run `pulley login --registry {registry}`")
        if not registry.local and username != manifest.author:
            warn(f"publishing as '{username}' to author '{manifest.author}'; the registry decides whether that is allowed")
        for line in registry.publish(staged, manifest.author, manifest.name, manifest.version, username, password,
                                     verbose=args.verbose):
            step(line)
    done(f"published {manifest.author}.{manifest.name}={manifest.version} to {registry}")


def cmd_pack(args) -> None:
    project = cfg.find_project()
    if not project:
        raise PulleyError(f"no {MANIFEST} here or in a parent directory")
    manifest = manifests.load(project)
    packaging.check(manifest)
    archive = Path(args.output or f"{manifest.author}.{manifest.name}-{manifest.version}.tar.gz").resolve()
    files = packaging.pack(manifest, archive)
    done(f"wrote {archive} ({len(files)} file(s), {archive.stat().st_size} bytes)")


def cmd_login(args) -> None:
    registry = Registry(_registry_name(args))
    if registry.local:
        raise PulleyError("local registries need no login")
    username = (args.username or input("Username: ")).strip().lower()
    password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Password: ")
    if not password:
        raise PulleyError("a password is required")
    registry.login(username, password)
    where = cfg.store_credentials(registry.spec, username, password)
    done(f"logged in to {registry} as {username} (credentials in {where})")


def cmd_logout(args) -> None:
    registry = _registry_name(args)
    if cfg.forget_credentials(registry):
        done(f"logged out of {registry}")
    else:
        say(f"not logged in to {registry}")


def cmd_whoami(args) -> None:
    registry = _registry_name(args)
    username, _ = cfg.credentials(registry)
    if not username:
        raise PulleyError(f"not logged in to {registry}")
    say(f"{username} @ {registry}")


def cmd_register(args) -> None:
    registry = Registry(_registry_name(args))
    password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Choose a password: ")
    if not args.password_stdin and getpass.getpass("Repeat it: ") != password:
        raise PulleyError("the passwords differ")
    registry.register(args.username.lower(), password)
    cfg.store_credentials(registry.spec, args.username.lower(), password)
    done(f"created user {args.username.lower()} on {registry} and logged in")


def cmd_registry(args) -> None:
    data = cfg.load_config()
    if args.reset:
        data.pop("registry", None)
        cfg.save_config(data)
        done(f"default registry reset to {cfg.DEFAULT_REGISTRY}")
    elif args.name:
        data["registry"] = args.name
        cfg.save_config(data)
        done(f"default registry is now {args.name}")
    else:
        project = cfg.find_project()
        manifest = manifests.load(project) if project else None
        say(cfg.default_registry(manifest.registry if manifest else None))


def cmd_config(args) -> None:
    keys = {"registry", "flywheel", "bin_dir"}
    data = cfg.load_config()
    if not args.key:
        for key in sorted(keys):
            say(f"{key} = {data.get(key, '')}")
        return
    if args.key not in keys:
        raise PulleyError(f"unknown setting '{args.key}' (settings: {', '.join(sorted(keys))})")
    if args.unset:
        data.pop(args.key, None)
    elif args.value is None:
        say(str(data.get(args.key, "")))
        return
    else:
        data[args.key] = args.value
    cfg.save_config(data)


def cmd_cache(args) -> None:
    root = cfg.cache_dir()
    if args.action == "dir":
        say(str(root))
    elif args.action == "clean":
        if root.exists():
            shutil.rmtree(root)
        done(f"emptied {root}")
    else:
        size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) if root.exists() else 0
        packages = list((root / "packages").glob("*/*/*/*")) if root.exists() else []
        say(f"{root}: {len(packages)} release(s), {size} bytes")
        for p in sorted(packages):
            step(f"{p.parent.parent.name}.{p.parent.name}={p.name} ({p.parent.parent.parent.name})")


def cmd_run(args) -> None:
    project = cfg.find_project()
    if not project:
        raise PulleyError(f"no {MANIFEST} here or in a parent directory")
    manifest = manifests.load(project)
    modules = project / LOCAL_MODULES
    commands = sorted(p.stem if tools.WINDOWS else p.name for p in (modules / tools.PROJECT_BIN).glob("*")
                      if not p.name.startswith(".")) if (modules / tools.PROJECT_BIN).is_dir() else []
    if not args.script:
        for name, command in manifest.scripts.items():
            say(f"{name}: {command}")
        for name in commands:
            say(f"{name}: a command of a dependency")
        return
    # Scripts can run the dependencies' commands by name.
    env = dict(os.environ, PATH=os.pathsep.join([str(modules / tools.PROJECT_BIN), os.environ.get("PATH", "")]))
    if args.script not in manifest.scripts:
        launcher = tools.project_command(modules, args.script)
        if not launcher:
            available = list(manifest.scripts) + commands
            raise PulleyError(f"no script or dependency command '{args.script}' "
                              f"(available: {', '.join(available) or 'none'})")
        sys.stdout.flush()
        sys.stderr.flush()
        sys.exit(tools.run_command(launcher, args.arguments, Path.cwd()))
    command = manifest.scripts[args.script]
    if args.arguments:
        command += " " + " ".join(_shell_quote(a) for a in args.arguments)
    step(command)
    sys.stdout.flush()  # before the script writes to the same output
    sys.stderr.flush()
    sys.exit(subprocess.call(command, shell=True, cwd=project, env=env))


def _shell_quote(text: str) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline([text])
    import shlex
    return shlex.quote(text)


def cmd_where(args) -> None:
    project = cfg.find_project()
    flywheel = cfg.find_flywheel()
    user = tools.scope(False)
    try:
        system = tools.scope(True)
        system_installs, system_bin = str(system.installs), str(system.bin)
    except PulleyError as exc:
        system_installs = system_bin = f"(unknown: {exc})"
    rows = [
        ("project", str(project) if project else "(none)"),
        ("dependencies", str(project / LOCAL_MODULES) if project else "(none)"),
        ("user installs", str(user.installs)),
        ("user commands", str(user.bin) + ("" if tools.on_path(user.bin) else " (not on PATH)")),
        ("system installs", system_installs),
        ("system commands", system_bin),
        ("flywheel", str(flywheel) if flywheel else "(not found on PATH)"),
        ("registry", cfg.default_registry(manifests.load(project).registry if project else None)),
        ("config", str(cfg.config_dir())),
        ("cache", str(cfg.cache_dir())),
    ]
    for key, value in rows:
        say(f"{key + ':':17} {value}")


# Argument parsing ------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    prog = Path(sys.argv[0]).name if sys.argv and Path(sys.argv[0]).name in ("pulley", "ppm") else "pulley"
    main = argparse.ArgumentParser(
        prog=prog,
        description="pulley, the official flywheel package manager by polymorphia."
    )
    main.add_argument("--version", action="version", version=f"pulley {__version__}")
    main.add_argument("-q", "--quiet", action="store_true", help="print only results and errors")
    commands = main.add_subparsers(dest="command", metavar="command")
    # -q also works after the command name.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="print only results and errors")

    def command(name, function, help_text, aliases=()):
        sub = commands.add_parser(name, help=help_text, aliases=list(aliases), description=help_text,
                                  parents=[common])
        sub.set_defaults(function=function)
        return sub

    def registry_option(sub):
        sub.add_argument("--registry", help="Distrobase registry (host, URL, or #/local/path)")

    def scope_options(sub, user=True):
        if user:
            sub.add_argument("--user", action="store_true", help="installs for this user")
        sub.add_argument("--system", "-g", "--global", dest="system", action="store_true",
                         help="installs for the Flywheel installation (every user of it)")

    def install_options(sub):
        registry_option(sub)
        sub.add_argument("--production", action="store_true", help="leave out dev-dependencies")
        sub.add_argument("-v", "--verbose", action="store_true", help="show resolution steps")

    sub = command("init", cmd_init, "create pulley.toml in the current (or given) directory")
    sub.add_argument("directory", nargs="?")
    for s in (sub, command("new", cmd_new, "create a new package directory")):
        if s is not sub:
            s.add_argument("directory")
        s.add_argument("--name")
        s.add_argument("--author", help="your Distrobase username")
        s.add_argument("--main", help="the main module file (default <name>.fw)")
        s.add_argument("--template", choices=["package", "game"], default="package",
                       help="what to start from: a package (default) or a game using Aperture")
        s.add_argument("--game", dest="template", action="store_const", const="game",
                       help="the same as --template game")
        s.set_defaults(tests=False)

    def add_options(sub, packages_nargs):
        sub.add_argument("packages", nargs=packages_nargs, metavar="package",
                         help="author.name, author.name=^1.2, author.name@registry, or alias=author.name")
        sub.add_argument("--as", dest="alias", help="import name (default: the distribution name)")
        sub.add_argument("-D", "--dev", action="store_true", help="add as a dev-dependency")
        sub.add_argument("--path", help="a local package directory")
        sub.add_argument("--git", help="a git repository URL")
        sub.add_argument("--rev", help="git branch, tag, or commit")
        sub.add_argument("--no-install", action="store_true", help="only change pulley.toml")
        install_options(sub)

    add_options(command("add", cmd_add, "add dependencies to this project and install them"), "*")
    sub = command("install", cmd_install, "install this project's dependencies, or install packages' commands "
                  "for this user (--system: for the Flywheel installation)", ["i", "get"])
    add_options(sub, "*")
    sub.add_argument("--frozen", action="store_true", help="fail if pulley.lock is out of date (for CI)")
    sub.add_argument("--force", action="store_true", help="take over commands that another install has")
    scope_options(sub, user=False)

    sub = command("uninstall", cmd_uninstall, "uninstall packages installed with `pulley install author.name`")
    sub.add_argument("names", nargs="+", metavar="name", help="the install's name, author.name, or a command")
    scope_options(sub, user=False)
    sub = command("remove", cmd_remove, "remove dependencies from this project", ["rm"])
    sub.add_argument("names", nargs="+", metavar="name")
    install_options(sub)
    sub = command("update", cmd_update, "move dependencies to the newest versions their constraints allow "
                  "(--user/--system: update installs)", ["up", "upgrade"])
    sub.add_argument("names", nargs="*", metavar="name")
    install_options(sub)
    scope_options(sub)
    sub.set_defaults(force=False)

    sub = command("list", cmd_list, "list this project's packages, or installs (--user/--system)", ["ls"])
    scope_options(sub)
    sub.add_argument("-t", "--tree", action="store_true", help="show the dependency tree")
    sub = command("outdated", cmd_outdated, "show dependencies with newer releases")

    sub = command("info", cmd_info, "show a distribution's releases", ["view", "show"])
    sub.add_argument("address", help="author.name[@registry][=version]")
    registry_option(sub)

    sub = command("publish", cmd_publish, "publish this package to a Distrobase registry")
    registry_option(sub)
    sub.add_argument("-n", "--dry-run", action="store_true", help="show what would be published")
    sub.add_argument("-v", "--verbose", action="store_true", help="list every file")
    sub = command("pack", cmd_pack, "write the release archive without publishing it")
    sub.add_argument("-o", "--output", help="archive path (default author.name-version.tar.gz)")

    sub = command("login", cmd_login, "log in to a registry for publishing")
    sub.add_argument("username", nargs="?")
    sub.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    registry_option(sub)
    sub = command("logout", cmd_logout, "forget the credentials for a registry")
    registry_option(sub)
    sub = command("whoami", cmd_whoami, "show the user logged in to a registry")
    registry_option(sub)
    sub = command("register", cmd_register, "create a user account on a registry")
    sub.add_argument("username")
    sub.add_argument("--password-stdin", action="store_true")
    registry_option(sub)

    sub = command("registry", cmd_registry, "show or set the default registry")
    sub.add_argument("name", nargs="?")
    sub.add_argument("--reset", action="store_true", help=f"go back to {cfg.DEFAULT_REGISTRY}")
    # Not an argparse alias, so it takes no arguments and can only show.
    command("whereami", cmd_registry, "show the default registry").set_defaults(name=None, reset=False)
    sub = command("config", cmd_config, "show or change settings (registry, flywheel, bin_dir)")
    sub.add_argument("key", nargs="?")
    sub.add_argument("value", nargs="?")
    sub.add_argument("--unset", action="store_true")
    sub = command("cache", cmd_cache, "show or clean the download cache")
    sub.add_argument("action", nargs="?", choices=["list", "dir", "clean"], default="list")
    sub = command("run", cmd_run, "run a script from [scripts], or a dependency's command")
    sub.add_argument("script", nargs="?")
    sub.add_argument("arguments", nargs=argparse.REMAINDER)
    command("where", cmd_where, "show the directories and registry Pulley uses", ["paths"])
    return main


def main(argv: list[str] | None = None) -> int:
    global QUIET
    arguments = parser().parse_args(argv)
    QUIET = arguments.quiet
    if not getattr(arguments, "function", None):
        parser().print_help()
        return 2
    try:
        arguments.function(arguments)
    except PulleyError as exc:
        print(_paint("error: ", "31") + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    return 0


def entrypoint() -> None:
    sys.exit(main())
