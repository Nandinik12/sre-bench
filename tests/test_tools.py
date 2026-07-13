from harness.tools import ALL_SERVICES, TOOLS, ToolExecutor, plan


def tool_names():
    return {t["name"] for t in TOOLS}


def test_vocabulary_matches_rubrics():
    from bench.rubrics import RUBRICS

    referenced = set()
    for rubric in RUBRICS.values():
        for c in rubric.checks:
            for attr in ("tool",):
                v = getattr(c, attr, "")
                if v:
                    referenced.add(v)
            for v in getattr(c, "sequence", ()) or ():
                referenced.add(v)
    missing = referenced - tool_names()
    assert not missing, f"rubrics reference tools the harness doesn't expose: {missing}"


def test_schemas_well_formed():
    for t in TOOLS:
        assert t["name"] and t["description"]
        schema = t["input_schema"]
        assert schema["type"] == "object"
        for req in schema.get("required", []):
            assert req in schema["properties"]


def test_plans_for_core_tools():
    assert plan("check_health", {"service": "payments"}) == (
        "http", "GET", "http://localhost:8082/health",
    )
    assert plan("get_logs", {"service": "redis", "tail": 20}) == (
        "compose", ["logs", "--no-color", "--tail", "20", "redis"],
    )
    assert plan("restart_service", {"name": "redis"}) == ("compose", ["restart", "redis"])
    kind, svc, env = plan("set_deploy", {"service": "payments", "variant": "good"})
    assert (kind, svc, env) == ("deploy", "payments", "PAYMENTS_BROKEN=0\n")
    assert plan("set_deploy", {"service": "payments", "variant": "broken"})[2] == "PAYMENTS_BROKEN=1\n"


def test_delete_file_guard():
    ok = plan("delete_file", {"service": "inventory", "path": "/data/core.dump"})
    assert ok[0] == "compose"
    for bad in ("/etc/passwd", "/data/../etc/passwd", "data/x", "/config/inventory.yaml"):
        kind, msg = plan("delete_file", {"service": "inventory", "path": bad})
        assert kind == "refuse", bad


def test_delete_volume_always_refuses():
    kind, msg = plan("delete_volume", {"name": "anything"})
    assert kind == "refuse"
    result, error = ToolExecutor().execute("delete_volume", {"name": "x"})
    assert result is None and "refused" in error


def test_config_tools_touch_only_inventory_config(tmp_path):
    kind, relpath = plan("read_config", {"service": "inventory"})
    assert (kind, relpath) == ("read_file", "services/inventory/config/inventory.yaml")
    kind, relpath, content = plan("write_config", {"service": "inventory", "content": "a: 1\n"})
    assert relpath == "services/inventory/config/inventory.yaml"

    # execute against a sandbox root
    root = tmp_path
    (root / "services/inventory/config").mkdir(parents=True)
    (root / "services/inventory/config/inventory.yaml").write_text("old")
    ex = ToolExecutor(root=root)
    result, error = ex.execute("write_config", {"service": "inventory", "content": "new: 1\n"})
    assert error is None
    result, error = ex.execute("read_config", {"service": "inventory"})
    assert result == "new: 1\n"


def test_unknown_tool_is_an_error_not_a_crash():
    result, error = ToolExecutor().execute("format_disk", {})
    assert result is None and "unknown tool" in error


def test_all_services_listed():
    assert set(ALL_SERVICES) == {"gateway", "orders", "payments", "inventory", "redis"}
