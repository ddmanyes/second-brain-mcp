"""Safety regressions use synthetic DSNs and mocked Docker; never connect."""

import pytest

from tests.support.pg_test_safety import (
    reject_external_postgres_settings,
    validate_owned_dsn,
)

TARGET = dict(database="sb_test_0123456789ab", port=49123, user="postgres", password="secret")
DSN = "postgresql://postgres:secret@127.0.0.1:49123/sb_test_0123456789ab?connect_timeout=5"


def test_owned_target_allowed():
    validate_owned_dsn(DSN, **TARGET)


@pytest.mark.parametrize("dsn", [
    "postgresql://postgres:secret@127.0.0.1:5434/postgres?connect_timeout=5",
    DSN.replace("sb_test_0123456789ab", "sb_personal"),
    DSN.replace("sb_test_0123456789ab", "sb_lab"),
    DSN.replace("sb_test_0123456789ab", "production_test"),
    DSN.replace("127.0.0.1", "localhost"),
    DSN.replace("49123", "5432"),
    DSN.replace("postgres:secret", "other:secret"),
    DSN + "&host=production.example",
    DSN + "&hostaddr=192.0.2.1",
    DSN + "&dbname=postgres",
    DSN + "&service=production",
    DSN + "&options=-csearch_path%3Dpublic",
    "host=127.0.0.1 dbname=postgres",
    "not a DSN secret",
    "postgresql://postgres:secret@127.0.0.1:999999/postgres",
    DSN.replace("sb_test_0123456789ab", "%70ostgres"),
])
def test_unsafe_dsn_rejected_without_credentials(dsn):
    with pytest.raises(RuntimeError) as exc:
        validate_owned_dsn(dsn, **TARGET)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("settings", [
    {"SB_PG_TEST_DSN": DSN}, {"SB_PG_TEST_DSN": ""},
    {"PGSERVICE": "production"}, {"PGHOSTADDR": "192.0.2.1"},
    {"PGOPTIONS": "-csearch_path=public"},
])
def test_external_settings_rejected(settings):
    with pytest.raises(RuntimeError):
        reject_external_postgres_settings(settings)


def test_no_external_settings_allowed():
    reject_external_postgres_settings({"PATH": "/bin"})


def test_harness_rejects_override_before_docker(monkeypatch):
    from tests.support import postgres_harness

    monkeypatch.setenv("SB_PG_TEST_DSN", "postgresql://secret@localhost/postgres")
    calls = []
    monkeypatch.setattr(postgres_harness, "_docker", lambda *a, **kw: calls.append(a))
    with pytest.raises(RuntimeError, match="External PostgreSQL"):
        postgres_harness.DisposablePostgres().start()
    assert calls == []


def test_dsn_requires_verified_ownership(monkeypatch):
    from tests.support.postgres_harness import DisposablePostgres

    harness = DisposablePostgres()
    harness.port = 49123

    def reject():
        raise RuntimeError("refusing unowned PostgreSQL test container")

    monkeypatch.setattr(harness, "_verify_container", reject)
    with pytest.raises(RuntimeError, match="unowned"):
        harness.dsn()


@pytest.mark.parametrize("defect", ["owner", "image", "mount", "bind", "tmpfs", "host", "port"])
def test_harness_rejects_untrusted_container_metadata(monkeypatch, defect):
    from tests.support.postgres_harness import IMAGE, LABEL_KEY, DisposablePostgres

    harness = DisposablePostgres()
    harness.port = 49123
    info = {
        "Config": {"Labels": {LABEL_KEY: harness.owner_token}, "Image": IMAGE},
        "Mounts": [{"Type": "tmpfs"}],
        "HostConfig": {"Tmpfs": {"/var/lib/postgresql/data": "rw"}},
        "NetworkSettings": {"Ports": {"5432/tcp": [
            {"HostIp": "127.0.0.1", "HostPort": "49123"}
        ]}},
    }
    if defect == "owner":
        info["Config"]["Labels"][LABEL_KEY] = "another-owner"
    elif defect == "image":
        info["Config"]["Image"] = "production"
    elif defect == "mount":
        info["Mounts"] = [{"Type": "volume"}]
    elif defect == "bind":
        info["HostConfig"]["Binds"] = ["/production:/data"]
    elif defect == "tmpfs":
        info["HostConfig"]["Tmpfs"] = {}
    elif defect == "host":
        info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
    elif defect == "port":
        info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"] = "5434"
    monkeypatch.setattr(harness, "_inspect", lambda: info)
    with pytest.raises(RuntimeError):
        harness._verify_container()
