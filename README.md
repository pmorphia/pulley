# Pulley

Pulley is the package manager for [Flywheel](../fw). It installs Flywheel packages from
[Distrobase](https://dstbs.wednesware.org) registries, using
[Hydrogen](https://github.com/Wednesware/Hydrogen) to resolve, download, and publish
distributions. The registry is `wednesware.org` unless you choose another.

Pulley installs Flywheel packages only. A Flywheel package has a `pulley.toml` at its root; a
registry distribution, git repository, or directory without one (a Hydrogen distribution, for
example) is refused with `is not a Flywheel package`, and nothing is recorded or installed.

The command is `pulley`, also installed as `ppm`. (Both names are taken on PyPI, so the
distribution is called `fw-pulley`.)

```sh
pipx install ./pulley        # or: pip install ./pulley
pulley pull driveshaft       # polymorphia.driveshaft: `import driveshaft` in every program
pulley pull driveshaft -b    # only its command, `driveshaft`
pulley new app && cd app     # a project: list dependencies in pulley.toml, then
pulley pull                  # install them into .flywheel-modules
```

A name without an author (`driveshaft`) is a polymorphia package (`polymorphia.driveshaft`).

Requirements: Python 3.12+ and `git` for git dependencies. Hydrogen is bundled with Pulley (as
`pulley.hydrogen`; see `pulley/hydrogen/PULLEY.md`) rather than installed from PyPI. Optionally,
install `keyring` (`pip install "fw-pulley[keyring]"`) to keep registry passwords in the system
keyring.

## Where packages go

| Install | Directory | Used by |
|---|---|---|
| `pulley pull` in a project | `./.flywheel-modules/` in the project (next to `pulley.toml`) | programs in that project |
| `pulley pull <package>` | its own environment, linked from `modules/` of the Flywheel installation | every program run by that `flywheel` |
| `pulley pull <package> -b` | its own environment, with launchers in `~/.local/bin` (`--system`: next to `flywheel`) | you, from the shell |

`pulley pull <package>` never reads or changes a `pulley.toml`, inside a project or not. Each
pulled package gets its own environment (`~/.local/share/pulley/installs/<name>`, or
`<flywheel>/pulley/installs` with `--system`) holding it and its dependencies, so pulled packages
never share or clash over dependencies. As a library, it is the link
`<flywheel>/modules/<name>.link`: any program can `import <name>`, and only that name; its own
dependencies stay in its environment. When the package has `[commands]`, Pulley asks whether to
install them too (`-y` says yes without asking; without a terminal, it doesn't, and says how).
`-b` installs only the commands. Pulling again adds to what is installed; `pulley update --user`
(or `--system`) moves pulls to newer releases, and `pulley uninstall <name>` removes one.

The installation directory is the one that holds `bin/flywheel` (or the directory of the
`flywheel` executable when it is not in a `bin` directory). Pulley finds `flywheel` on `PATH`;
`pulley config flywheel /path/to/flywheel`, `PULLEY_FLYWHEEL`, or `FLYWHEEL_HOME` (which the
compiler also honours, as `$FLYWHEEL_HOME/modules`) override it. `pulley where` shows the
directories in use.

The Flywheel compiler searches the nearest `.flywheel-modules` above the importing file, then
the installation's `modules`. Each package is a directory named by its import name, or a
`<name>.link` file holding the path of a path dependency. Pulley owns these entries: it writes a
`.pulley.json` record into each package directory and only removes entries it installed. Add
`.flywheel-modules/` to `.gitignore` (`pulley init` does) and commit `pulley.toml` and
`pulley.lock`.

## pulley.toml

```toml
[package]
name = "app"              # the distribution name
author = "danny"          # your Distrobase username; publishes as danny.app
version = "0.1.0"
description = "An example"
main = "app.fw"           # what `import app` loads (default <name>.fw)
license = "MIT"
exclude = ["drafts/*"]    # files left out of published releases

[registry]
default = "wednesware.org"    # optional, for this project

[dependencies]
shapes = "wednesware.shapes=^1.2"                  # import name = Distrobase address [= constraint]
text = "acme.text@example.org=~2.0"                # from another registry
vendored = "acme.vendored#/srv/registry=1.0"       # a local (filesystem) registry
geometry = { distribution = "acme.geometry", version = ">=1.0, <3", registry = "example.org" }
helpers = { path = "../helpers" }                  # a directory on disk
parser = { git = "https://example.com/parser.git", rev = "v2.1" }

[dev-dependencies]
fixtures = "danny.fixtures"

[scripts]
test = "flywheel test"
```

The key of each dependency is its import name (`import shapes`), so one distribution can be
imported under a different name. Dependencies of dependencies are installed under the names
their packages give them; two packages that want the same import name for different
distributions are reported as a conflict.

### Version constraints

| Constraint | Allows |
|---|---|
| `*`, `latest`, or none | any release |
| `1.2.3` or `=1.2.3` | exactly that release |
| `^1.2.3` | `>=1.2.3, <2.0.0` (`^0.2.3` is `>=0.2.3, <0.3.0`) |
| `~1.2.3` | `>=1.2.3, <1.3.0` (`~1` is `>=1, <2`) |
| `1.2.*` | `>=1.2, <1.3` |
| `>=1.0, <2`, `!=1.4` | every comparison together |

Versions are dotted numbers with an optional pre-release (`2.0.0-beta.1`); pre-releases are
chosen only when a constraint names one. `pulley pull <package>` without a constraint pulls `^`
the newest release, so `pulley update` stays on compatible ones.

## Resolution and the lock file

Every distribution resolves to one version for the whole project: the highest release that
satisfies the constraints of the project and of every package that depends on it. When no release
satisfies all of them, Pulley names each requirement and who made it:

```text
error: no version of acme.mathx on wednesware.org satisfies every requirement:
^2.0 (by app); ~1.0 (by acme.geo=1.0.0). Available: 1.0.0, 1.1.0, 2.0.0
```

A failed `pulley pull` leaves `pulley.lock` and `.flywheel-modules` unchanged. `pulley.lock` records the chosen version of every
package with the SHA-256 of its release archive (and the commit of git dependencies). Later
installs reuse the locked versions while they still satisfy `pulley.toml`, and refuse an archive
whose checksum differs from the lock. `pulley update` moves packages to the newest versions their
constraints allow; `pulley pull --frozen` fails instead of changing the lock (for CI).

Downloads are verified against the SHA-256 that the registry publishes and cached (see
`pulley cache`), so each release is fetched once per machine.

## Commands

| Command | Does |
|---|---|
| `pulley init [dir]`, `pulley new <dir>` | create `pulley.toml` (and a main module, a test, `.gitignore`); `--game` (or `--template game`) starts a game instead: [Aperture](https://github.com/pmorphia/aperture) as a dependency, a playable game loop, an `assets/` directory, and a `start` script |
| `pulley pull` (`p`) | in a project, install everything in `pulley.toml`; `--production` skips dev-dependencies; `--frozen` fails if the lock is out of date |
| `pulley pull <package>...` | install packages as libraries for every program (`name`, `author.name`, `author.name=^1.2`, `author.name@registry`, `--as name`, `--path dir`, `--git url --rev ref`); asks about commands; `-y` installs them too; `-b`/`--bin` installs only the commands; `--system` puts commands next to `flywheel`; `--force` takes over a command or import name |
| `pulley uninstall <name>...` | remove a pull (by name, `author.name`, or command): its environment, library link, and launchers |
| `pulley remove <name>...` (`rm`) | remove dependencies from `pulley.toml` (and packages only they needed) |
| `pulley update [name...]` (`up`) | re-resolve to the newest allowed versions; `--user`/`--system` update pulls |
| `pulley list [--tree]` (`ls`), `pulley outdated` | what is installed (`--user`/`--system`: pulls); what has newer releases |
| `pulley info <address>` (`view`) | a distribution's releases, artifacts, sizes, and checksums |
| `pulley publish [--dry-run]` | publish the project as `author.name=version` |
| `pulley pack [-o file]` | write the release archive without publishing |
| `pulley login [user]`, `logout`, `whoami`, `register <user>` | registry accounts |
| `pulley registry [name]` / `--reset` | show or set the default registry (`whereami` only shows it) |
| `pulley config [key] [value]` | settings: `registry`, `flywheel`, `bin_dir` |
| `pulley run [script] [args]` | run a `[scripts]` entry in the project directory, or a dependency's command |
| `pulley cache [list\|dir\|clean]`, `pulley where` | the download cache; the directories in use |

Every command that talks to a registry accepts `--registry`. `-q` prints only results and errors;
`-v` shows resolution steps.

## Registries

A registry is named as Hydrogen names it: a host (`wednesware.org`, served from
`https://dstbs.wednesware.org`), `localhost:port` or `127.0.0.1:port`, a full `http(s)://`
URL, or `#/path/to/directory` for a local registry on disk (releases are stored as
`<path>/dstbs/<author>/<name>/<version>.tar.gz`, as Hydrogen publishes them). The registry used
is, most specific first: `--registry`, a dependency's own registry, `[registry] default` in
`pulley.toml`, `PULLEY_REGISTRY`, `pulley registry <name>`, and `wednesware.org`.

Pulley always passes Hydrogen fully qualified addresses (`author.name@registry=version`), so
Hydrogen's own home-registry setting does not affect it.

### Publishing

```sh
pulley register danny          # once, if you have no account
pulley login danny
pulley publish --dry-run       # list the files that would be published
pulley publish
```

A release contains the project's files except `.git`, `.flywheel-modules`, `pulley.lock`, and
`[package] exclude` patterns. Publishing checks that the name, author, version, and main module
are set and that no dependency is a local path or the package itself. Registry credentials are kept per registry: in
the system keyring when `keyring` is installed, otherwise in a file readable only by you
(`credentials.json` in Pulley's configuration directory). `PULLEY_AUTH_USER` and
`PULLEY_AUTH_PASSWORD` (or Hydrogen's `DISTROBASE_AUTH_USER` / `DISTROBASE_AUTH_PASSWORD`)
override them, for CI.

Distrobase has no search endpoint, so Pulley has no `search` command.

A registry that lists only a distribution's `latest` release (rather than every release) still
works: exact versions, and constraints that the latest release satisfies, resolve; other
constraints report which releases are known.

## Files and environment

| Variable | Default |
|---|---|
| `PULLEY_REGISTRY` | the default registry |
| `PULLEY_CONFIG_DIR` | `~/.config/pulley` (`%APPDATA%\pulley` on Windows) |
| `PULLEY_CACHE_DIR` | `~/.cache/pulley` (`%LOCALAPPDATA%\pulley\cache`) |
| `PULLEY_FLYWHEEL` | the `flywheel` executable (default: on `PATH`) |
| `FLYWHEEL_HOME` | the Flywheel installation; pulled libraries are linked from `$FLYWHEEL_HOME/modules` |
| `PULLEY_DATA_DIR` | `~/.local/share/pulley` (`%LOCALAPPDATA%\pulley`): pulls for this user |
| `PULLEY_BIN_DIR` | `~/.local/bin`: commands pulled for this user (also `pulley config bin_dir`) |
| `NO_COLOR` | plain output |

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e .
FLYWHEEL=../build/flywheel .venv/bin/python -m unittest discover tests
```

The tests use a local registry and an in-memory Distrobase server (`tests/distrobase.py`, with
the same API Hydrogen uses), and compile and run programs against the installed packages when
`FLYWHEEL` points to a compiler.
