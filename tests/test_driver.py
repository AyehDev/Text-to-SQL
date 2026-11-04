"""Neo4j config and driver wiring. No server needed: creating a driver does not connect."""

import pytest
from pydantic import ValidationError

from schemagraph import config as cfg
from schemagraph.graph import driver as drv

BASE = {
    "postgres": {
        "admin": {"user": "a", "password_env": "A_PW"},
        "readonly": {"user": "r", "password_env": "R_PW"},
    }
}


def test_neo4j_section_is_optional():
    assert cfg.AppConfig.model_validate(BASE).neo4j is None


def test_neo4j_defaults_and_password_from_env(monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "s3cret")
    n = cfg.AppConfig.model_validate({**BASE, "neo4j": {}}).neo4j
    assert (n.uri, n.user, n.database) == ("bolt://localhost:7687", "neo4j", "neo4j")
    assert n.password.get_secret_value() == "s3cret"
    assert "s3cret" not in repr(n)  # SecretStr keeps it out of logs


def test_missing_password_variable_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="NEO4J_PASSWORD"):
        cfg.Neo4jConfig().password


def test_unknown_keys_are_rejected():
    with pytest.raises(ValidationError):
        cfg.AppConfig.model_validate({**BASE, "neo4j": {"passwrd": "x"}})


@pytest.fixture
def with_config(monkeypatch):
    def apply(raw):
        monkeypatch.setattr(drv, "get_config", lambda: cfg.AppConfig.model_validate(raw))
        drv.get_driver.cache_clear()
    yield apply
    drv.close_driver()
    drv.get_driver.cache_clear()


def test_no_neo4j_section_explains_itself(with_config):
    with_config(BASE)
    with pytest.raises(RuntimeError, match="no 'neo4j' section"):
        drv.get_driver()


def test_driver_is_created_lazily_and_shared(with_config, monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")
    with_config({**BASE, "neo4j": {"uri": "bolt://127.0.0.1:1"}})  # nothing listens here
    d = drv.get_driver()
    assert d is drv.get_driver()


def test_check_connection_reports_where_it_tried(with_config, monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")
    with_config({**BASE, "neo4j": {"uri": "bolt://127.0.0.1:1"}})
    with pytest.raises(RuntimeError, match=r"cannot connect to Neo4j at bolt://127.0.0.1:1"):
        drv.check_connection()
