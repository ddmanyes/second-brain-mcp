"""Owned, disposable PostgreSQL container harness for multiuser/RLS tests.

Mirrors Evo_PRISM_lab_access/tests/support/postgres_harness.py's shape
(owned-container verification, tmpfs data dir, loopback-only port) — this is
the "isolated test DB" the lab-open plan requires: Phase 4 tests must never
run against the real lcdda Postgres at 127.0.0.1:5434.
"""

from __future__ import annotations

import json
import secrets
import string
import subprocess
import time
import uuid
from dataclasses import dataclass


IMAGE = "pgvector/pgvector:0.8.6-pg16-bookworm"
LABEL_KEY = "second-brain.test-owner"


def _secret() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


@dataclass(frozen=True)
class RoleConnection:
    user: str
    password: str


class DisposablePostgres:
    """One throwaway Postgres 16 + pgvector container, torn down by stop()."""

    def __init__(self) -> None:
        token = uuid.uuid4().hex
        self.owner_token = token
        self.name = f"sb-multiuser-test-{token[:12]}"
        self.database = f"sb_test_{token[:12]}"
        self.postgres_password = _secret()
        self.sb_app = RoleConnection("sb_app", _secret())
        self.host = "127.0.0.1"
        self.port: int | None = None

    def start(self) -> None:
        _docker(
            "run", "--detach", "--rm", "--pull", "never",
            "--name", self.name,
            "--label", f"{LABEL_KEY}={self.owner_token}",
            "--cpus", "1", "--memory", "512m",
            "--tmpfs", "/var/lib/postgresql/data:rw,noexec,nosuid,size=384m",
            "--publish", "127.0.0.1::5432",
            "--env", f"POSTGRES_PASSWORD={self.postgres_password}",
            "--env", f"POSTGRES_DB={self.database}",
            IMAGE,
        )
        try:
            self._verify_container()
            port_output = _docker("port", self.name, "5432/tcp").stdout.strip()
            host, raw_port = port_output.rsplit(":", 1)
            if host != self.host:
                raise RuntimeError(f"Postgres test port is not loopback-only: {port_output}")
            self.port = int(raw_port)
            self._wait_ready()
        except Exception:
            self.stop()
            raise

    def _inspect(self) -> dict:
        result = _docker("inspect", self.name)
        records = json.loads(result.stdout)
        if len(records) != 1:
            raise RuntimeError("expected exactly one owned PostgreSQL container")
        return records[0]

    def _verify_container(self) -> None:
        info = self._inspect()
        labels = info.get("Config", {}).get("Labels") or {}
        if labels.get(LABEL_KEY) != self.owner_token:
            raise RuntimeError("refusing unowned PostgreSQL test container")
        if info.get("Config", {}).get("Image") != IMAGE:
            raise RuntimeError("unexpected PostgreSQL test image")
        mounts = info.get("Mounts") or []
        if any(mount.get("Type") != "tmpfs" for mount in mounts):
            raise RuntimeError("host-backed mount detected in PostgreSQL test container")
        host_config = info.get("HostConfig") or {}
        if host_config.get("Binds"):
            raise RuntimeError("bind mount detected in PostgreSQL test container")
        tmpfs = host_config.get("Tmpfs") or {}
        if "/var/lib/postgresql/data" not in tmpfs:
            raise RuntimeError("PostgreSQL test data directory is not tmpfs")
        ports = info.get("NetworkSettings", {}).get("Ports", {}).get("5432/tcp") or []
        if len(ports) != 1 or ports[0].get("HostIp") != self.host:
            raise RuntimeError("PostgreSQL test port is not uniquely bound to loopback")
        if self.port is not None and int(ports[0].get("HostPort", 0)) != self.port:
            raise RuntimeError("PostgreSQL test port changed after ownership verification")

    def _psql(self, sql: str, *, check: bool = False):
        return _docker(
            "exec", self.name, "psql", "-v", "ON_ERROR_STOP=1",
            "-U", "postgres", "-d", self.database, "-c", sql, check=check,
        )

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._psql("SELECT 1").returncode == 0:
                return
            time.sleep(0.2)
        raise RuntimeError("disposable PostgreSQL did not become ready")

    def dsn(self, *, role: str = "postgres") -> str:
        """A postgresql:// DSN for either the superuser or the (schema-created)
        sb_app role. sb_app's password is set by tests via _bootstrap_sb_app()
        or by the RLS migration's own ALTER ROLE step."""
        if self.port is None:
            raise RuntimeError("disposable PostgreSQL is not started")
        if role == "postgres":
            user, password = "postgres", self.postgres_password
        elif role == "sb_app":
            user, password = self.sb_app.user, self.sb_app.password
        else:
            raise ValueError("role must be 'postgres' or 'sb_app'")
        return (
            f"postgresql://{user}:{password}@{self.host}:{self.port}/{self.database}"
            "?connect_timeout=5"
        )

    def set_sb_app_password(self) -> None:
        """sb_app is created (no password) by postgres_rls_schema.sql; tests
        set a password afterward the same way a real deployment would, out of
        band from the checked-in schema file."""
        self._psql(
            f"ALTER ROLE sb_app PASSWORD '{self.sb_app.password}'",
            check=True,
        )

    def reset(self) -> None:
        """Drop and recreate the public schema for a clean slate between tests."""
        self._verify_container()
        result = self._psql(
            "DROP SCHEMA public CASCADE; CREATE SCHEMA public; "
            "GRANT ALL ON SCHEMA public TO postgres; GRANT USAGE ON SCHEMA public TO PUBLIC;"
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to reset disposable PostgreSQL public schema: {result.stderr}")

    def stop(self) -> None:
        try:
            info = self._inspect()
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return
        labels = info.get("Config", {}).get("Labels") or {}
        if labels.get(LABEL_KEY) != self.owner_token:
            raise RuntimeError("refusing to remove unowned PostgreSQL container")
        _docker("rm", "--force", self.name)
