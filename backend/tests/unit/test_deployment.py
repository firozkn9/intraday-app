"""Deployment invariants (Phase 13 security review): secrets reach only the engine."""

from pathlib import Path

import yaml

from intraday.core.env import is_placeholder

ROOT = Path(__file__).resolve().parents[3]
SECRET_VARS = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "ANTHROPIC_API_KEY", "SMTP_PASSWORD")


def services() -> dict[str, dict]:  # type: ignore[type-arg]
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]


def test_broker_and_ai_keys_only_in_engine() -> None:
    for name, svc in services().items():
        env = svc.get("environment") or {}
        leaked = [v for v in SECRET_VARS if v in env]
        if name == "engine":
            assert set(SECRET_VARS) <= set(env)
        else:
            assert not leaked, f"{name} receives {leaked}"


def test_only_engine_and_edge_leave_the_internal_network() -> None:
    svcs = services()
    for name in ("api", "postgres", "redis"):
        assert svcs[name]["networks"] == ["backend"], name
    assert "egress" in svcs["engine"]["networks"]


def test_backend_containers_are_hardened() -> None:
    for name in ("api", "engine"):
        svc = services()[name]
        assert svc.get("read_only") is True
        assert "no-new-privileges:true" in svc.get("security_opt", [])


def test_deploy_script_refuses_live_settings() -> None:
    script = (ROOT / "infrastructure" / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert 'TRADING_MODE:-}" = "PAPER"' in script
    assert "https://paper-api.alpaca.markets" in script


def test_env_example_has_only_placeholders() -> None:
    values = dict(line.split("=", 1) for line in
                  (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
                  if "=" in line and not line.startswith("#"))
    for key in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "SESSION_SECRET"):
        assert values[key] == "" or is_placeholder(values[key]), key
