"""
verify_deploy.py
Phase 1.1 — container build & compose verification suite (static; Docker is not needed).

Covers:
  • requirements.lock pins every requirement with hashes; requirements-build.lock
    pins only setuptools
  • Dockerfile: base image pinned by digest, hash-checked binary-only install
    (pyaes the one source build), only allowlisted files copied, non-root user,
    runtime is the default target, API command
  • compose: exactly migrate / api / worker / cloudflared; every service hardened
    (read-only root, no capabilities, no-new-privileges, init, log rotation);
    no published ports, host networking or privileged mode
  • one database: the same SQLite URL inside the data bind mount for migrate, api
    and worker; bind mounts never create host paths; api and worker wait for a
    successful migration
  • the Telegram session is mounted in the worker only; one worker (fixed name, no
    replicas); restart policies match the worker's exit codes; shutdown grace
    covers the worker's flush
  • networks: the API is only on the internal network; secrets split per service
    and the env templates hold no values
  • .dockerignore keeps the files the build and the test stage need

Reads files only; nothing touches a database or the network.
Run: python verify_deploy.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DATA_DIR = "/var/lib/placement/data"
SESSION_DIR = "/var/lib/placement/telegram"
APP_SERVICES = ("migrate", "api", "worker")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _interpolate(value):
    """Resolve compose's ${VAR:-default} to its default, recursively."""
    if isinstance(value, str):
        return re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", value)
    if isinstance(value, list):
        return [_interpolate(item) for item in value]
    if isinstance(value, dict):
        return {key: _interpolate(item) for key, item in value.items()}
    return value


def _compose() -> dict:
    return _interpolate(yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8")))


def _dockerfile_instructions() -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs with line continuations joined and comments dropped."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    joined = re.sub(r"\\\n", " ", "\n".join(lines))
    instructions = []
    for line in joined.splitlines():
        if line.strip():
            keyword, _, args = line.strip().partition(" ")
            instructions.append((keyword.upper(), " ".join(args.split())))
    return instructions


def _stages(instructions) -> dict[str, list[tuple[str, str]]]:
    stages: dict[str, list[tuple[str, str]]] = {}
    current = None
    for keyword, args in instructions:
        if keyword == "FROM":
            current = args.split(" AS ")[-1].strip()
            stages[current] = [(keyword, args)]
        elif current is not None:
            stages[current].append((keyword, args))
    return stages


def _pins(path: Path) -> dict[str, str]:
    """Package → the text of its entry (pin line plus hash lines)."""
    entries: dict[str, str] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==\S+", line)
        if match:
            current = match.group(1).lower().replace("_", "-")
            entries[current] = line
        elif current and line.startswith("    --hash="):
            entries[current] += "\n" + line
    return entries


def _bind_targets(service: dict) -> dict[str, dict]:
    targets = {}
    for volume in service.get("volumes", []):
        assert isinstance(volume, dict) and volume.get("type") == "bind", f"use long-syntax bind mounts: {volume!r}"
        targets[volume["target"]] = volume
    return targets


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_lock_files() -> None:
    lock = _pins(ROOT / "requirements.lock")
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        name = re.match(r"[A-Za-z0-9_.-]+", line).group(0).lower().replace("_", "-")
        assert name in lock, f"{name} from requirements.txt is missing from requirements.lock"
    for name, entry in lock.items():
        assert "--hash=sha256:" in entry, f"{name} has no hash in requirements.lock"
    for name in ("uvloop", "pyaes", "pyyaml"):
        assert name in lock, f"{name} missing (Linux-only, Telethon or verify_deploy dependency)"

    build = _pins(ROOT / "requirements-build.lock")
    assert list(build) == ["setuptools"], build.keys()
    assert "--hash=sha256:" in build["setuptools"]
    print(f"  [1] ✓ requirements.lock pins all {len(lock)} packages with hashes (every requirement covered); requirements-build.lock pins only setuptools")


def test_dockerfile() -> None:
    instructions = _dockerfile_instructions()
    args = {a.split("=", 1)[0]: a.split("=", 1)[1] for k, a in instructions if k == "ARG" and "=" in a}
    assert DIGEST.search(args["PYTHON_IMAGE"]) and args["PYTHON_IMAGE"].startswith("python:3.12"), args
    stages = _stages(instructions)
    assert list(stages) == ["builder", "app", "test", "runtime"], list(stages)
    for name in ("builder", "app"):
        assert stages[name][0][1].startswith("${PYTHON_IMAGE} AS"), f"{name} must build from the pinned image"
    assert stages["test"][0][1] == "app AS test" and stages["runtime"][0][1] == "app AS runtime"

    installs = [a for k, a in stages["builder"] if k == "RUN" and "pip install" in a]
    assert installs, "no pip install in the builder"
    for run in installs:
        for command in re.findall(r"pip install[^&]*", run):
            assert "--require-hashes" in command, command
            if "requirements.lock" in command:
                assert "--only-binary=:all:" in command and "--no-binary=pyaes" in command, command
                assert "--no-build-isolation" in command, command
    assert any("pip uninstall -y setuptools" in a for k, a in stages["builder"] if k == "RUN")

    allowed_sources = {
        "builder": {"requirements-build.lock", "requirements.lock"},
        "app": {"alembic.ini", "alembic/", "app/", "scripts/login_telegram.py"},
        "test": {".dockerignore", "Dockerfile", "docker-compose.yml", "requirements.txt", "requirements.lock",
                 "requirements-build.lock", "verify_*.py", "scripts/", "deploy/"},
        "runtime": set(),
    }
    for stage, body in stages.items():
        for keyword, arguments in body:
            if keyword in ("ADD",):
                raise AssertionError("use COPY, not ADD")
            if keyword == "COPY" and not arguments.startswith("--from="):
                sources = [part for part in arguments.split(" ") if not part.startswith("--")][:-1]
                unexpected = set(sources) - allowed_sources[stage]
                assert not unexpected, f"{stage} copies {unexpected}"

    app_users = [a for k, a in stages["app"] if k == "USER"]
    assert app_users == ["placement"], app_users
    assert not any(k == "USER" for k, _ in stages["runtime"] + stages["test"]), "USER must stay the app user"
    assert any("--uid 10001" in a and "--gid placement" in a for k, a in stages["app"] if k == "RUN")
    cmd = [a for k, a in stages["runtime"] if k == "CMD"]
    assert cmd and cmd[0].startswith('["uvicorn", "app.main:app"'), cmd
    assert '"--port", "8000"' in cmd[0] and '"--workers", "1"' in cmd[0], cmd
    print("  [2] ✓ Dockerfile: digest-pinned python:3.12 base, hash-checked binary-only install (pyaes the one source build), allowlisted COPYs, user 10001, runtime is the default target")


def test_compose_hardening() -> None:
    compose = _compose()
    services = compose["services"]
    assert set(services) == {"migrate", "api", "worker", "cloudflared"}, set(services)
    for name, service in services.items():
        assert service.get("read_only") is True, f"{name}: read_only"
        assert service.get("cap_drop") == ["ALL"], f"{name}: cap_drop"
        assert "no-new-privileges:true" in service.get("security_opt", []), f"{name}: no-new-privileges"
        assert service.get("init") is True, f"{name}: init"
        assert service.get("logging", {}).get("options", {}).get("max-size"), f"{name}: log rotation"
        for forbidden in ("ports", "privileged", "cap_add", "devices", "pid", "ipc", "user"):
            assert forbidden not in service, f"{name}: {forbidden} is not allowed"
        assert service.get("network_mode") in (None, "none"), f"{name}: network_mode"

    for name in APP_SERVICES:
        assert services[name]["image"] == "placement-engine:local", name
    assert services["migrate"]["build"] == {"context": ".", "target": "runtime"}
    for name in ("api", "worker"):
        assert "build" not in services[name] and services[name]["pull_policy"] == "never", name
    assert DIGEST.search(services["cloudflared"]["image"]), services["cloudflared"]["image"]
    assert services["cloudflared"]["command"] == ["tunnel", "--no-autoupdate", "run"]
    print("  [3] ✓ compose: migrate/api/worker/cloudflared, all read-only with no capabilities, no-new-privileges, init and log rotation; no ports, host network or privileges; cloudflared pinned by digest")


def test_one_database_and_migration_order() -> None:
    services = _compose()["services"]
    urls = {name: services[name]["environment"]["DATABASE_URL"] for name in APP_SERVICES}
    assert len(set(urls.values())) == 1, urls
    url = urls["api"]
    assert url.startswith("sqlite+aiosqlite:////") and url.split(":///", 1)[1].startswith(DATA_DIR + "/"), url

    for name in APP_SERVICES:
        mounts = _bind_targets(services[name])
        assert DATA_DIR in mounts, f"{name} lacks the data mount"
        assert mounts[DATA_DIR]["source"] == "/srv/placement/data", mounts[DATA_DIR]
    for name, service in services.items():
        for mount in _bind_targets(service).values():
            assert mount.get("bind", {}).get("create_host_path") is False, f"{name}: {mount['target']} may create host paths"

    migrate = services["migrate"]
    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert migrate["network_mode"] == "none" and migrate["restart"] == "no"
    for name in ("api", "worker"):
        assert services[name].get("depends_on") == {"migrate": {"condition": "service_completed_successfully"}}, name
    assert services["cloudflared"]["depends_on"] == {"api": {"condition": "service_healthy"}}
    assert "/health" in " ".join(services["api"]["healthcheck"]["test"])
    print("  [4] ✓ one SQLite URL inside the data mount for migrate, api and worker; bind mounts never create host paths; api/worker wait for migrate, cloudflared for a healthy api")


def test_single_worker_owns_the_session() -> None:
    services = _compose()["services"]
    for name, service in services.items():
        mounted = SESSION_DIR in _bind_targets(service)
        assert mounted == (name == "worker"), f"{name}: the session must be mounted in the worker only"
        if name != "worker":
            assert "TELEGRAM_SESSION_NAME" not in service.get("environment", {}), name

    worker = services["worker"]
    assert worker["command"] == ["python", "-m", "app.worker"]
    assert worker["environment"]["TELEGRAM_SESSION_NAME"].startswith(SESSION_DIR + "/")
    assert worker["container_name"] == "placement-worker"
    assert "deploy" not in worker and "scale" not in worker
    assert worker["restart"] == "on-failure", "exit 0 (Telegram disabled) must not loop"
    for name in ("api", "cloudflared"):
        assert services[name]["restart"] == "unless-stopped", name

    tree = ast.parse((ROOT / "app" / "worker.py").read_text(encoding="utf-8"))
    flush = next(
        node.value.value for node in tree.body
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "SHUTDOWN_FLUSH_TIMEOUT_SECONDS"
    )
    grace = int(worker["stop_grace_period"].rstrip("s"))
    assert grace >= flush + 10, f"stop_grace_period {grace}s must exceed the {flush:.0f}s flush"
    print(f"  [5] ✓ session mounted in the worker only; one worker (fixed name, no replicas, restart on-failure); {grace}s grace covers the {flush:.0f}s flush")


def test_networks_and_secrets() -> None:
    compose = _compose()
    services = compose["services"]
    networks = compose["networks"]
    assert networks["edge"].get("internal") is True
    assert services["api"]["networks"] == ["edge"]
    assert services["worker"]["networks"] == ["egress"]
    assert sorted(services["cloudflared"]["networks"]) == ["edge", "egress"]

    expected = {"api": "api.env", "worker": "worker.env", "cloudflared": "cloudflared.env"}
    for name, filename in expected.items():
        assert services[name]["env_file"] == [f"/srv/placement/secrets/{filename}"], services[name]["env_file"]
        assert (ROOT / "deploy" / "templates" / f"{filename}.example").is_file(), filename
    assert "env_file" not in services["migrate"]
    assert services["api"]["environment"]["APP_ENV"] == "production"

    non_secret = {"INGEST_ENVIRONMENTS": "TEST", "LLM_BASE_URL": "https://api.groq.com/openai/v1",
                  "LLM_MODEL": "openai/gpt-oss-20b"}
    for template in sorted((ROOT / "deploy" / "templates").glob("*.env.example")):
        for line in template.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, _, value = line.partition("=")
            assert value == "" or non_secret.get(key) == value, f"{template.name}: {key} must stay empty"
    api_template = (ROOT / "deploy" / "templates" / "api.env.example").read_text(encoding="utf-8")
    assert not re.search(r"^\s*#?\s*(TELEGRAM_|LLM_|GROQ_)", api_template, re.M), "api.env must not hold Telegram/LLM secrets"
    print("  [6] ✓ api only on the internal network, worker only on egress; one env file per service; templates hold no values (first boot INGEST_ENVIRONMENTS=TEST)")


def test_build_context_keeps_needed_files() -> None:
    from verify_security import _docker_excluded  # same .dockerignore matcher as Phase 0.6

    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    patterns = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    needed = [
        "requirements.lock", "requirements-build.lock", "Dockerfile", "docker-compose.yml", ".dockerignore",
        "verify_deploy.py", "deploy/templates/api.env.example", "deploy/templates/worker.env.example",
        "scripts/login_telegram.py", "app/worker.py",
    ]
    for path in needed:
        assert not _docker_excluded(path, patterns), f"{path} is excluded from the build context"
    for path in (".env", "placement_session.session", "dev.db", "dev.db-wal"):
        assert _docker_excluded(path, patterns), f"{path} would reach the build context"
    print("  [7] ✓ .dockerignore keeps the lock files, Dockerfile, compose file, deploy/ and the suites; secrets, sessions and databases stay out")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> None:
    print("\nRunning Phase 1.1 container build & compose verification suite…\n")

    test_lock_files()
    test_dockerfile()
    test_compose_hardening()
    test_one_database_and_migration_order()
    test_single_worker_owns_the_session()
    test_networks_and_secrets()
    test_build_context_keeps_needed_files()

    print("\n✅ Phase 1.1 Container Build & Compose Verified Successfully!")


if __name__ == "__main__":
    main()
