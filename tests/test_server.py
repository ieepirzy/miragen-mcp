import pytest
import server


# ── import smoke test ─────────────────────────────────────────────────────────

def test_module_imports():
    """Server module loads without errors (catches startup AttributeErrors)."""
    assert server.app is not None


# ── _safe_path ────────────────────────────────────────────────────────────────

def test_safe_path_normal(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    full, err = server._safe_path("agent", "subdir/file.txt")
    assert err is None
    assert full == (tmp_path / "agents" / "agent" / "subdir" / "file.txt").resolve()


def test_safe_path_dotdot(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    _, err = server._safe_path("agent", "../other/secret.txt")
    assert err == "ERROR: path traversal not allowed"


def test_safe_path_absolute(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    _, err = server._safe_path("agent", "/etc/passwd")
    assert err == "ERROR: path traversal not allowed"


def test_safe_path_nested_dotdot(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    _, err = server._safe_path("agent", "sub/../../escape")
    assert err == "ERROR: path traversal not allowed"


# ── _parse_registered_tools ───────────────────────────────────────────────────

def test_parse_plain_decorator():
    tools = server._parse_registered_tools(
        "from miragen import register\n\n"
        "@register\nasync def do_thing(ctx, x: str) -> str:\n"
        '    """Does a thing."""\n    return x\n'
    )
    assert len(tools) == 1
    assert tools[0]["name"] == "do_thing"
    assert tools[0]["description"] == "Does a thing."
    assert "ctx" in tools[0]["signature"]


def test_parse_named_decorator():
    tools = server._parse_registered_tools(
        "from miragen import register\n\n"
        '@register("speak_aloud")\nasync def tts(ctx, text: str) -> None:\n    pass\n'
    )
    assert len(tools) == 1
    assert tools[0]["name"] == "speak_aloud"


def test_parse_ignores_sync_functions():
    tools = server._parse_registered_tools(
        "@register\ndef sync_fn(ctx):\n    pass\n"
    )
    assert tools == []


def test_parse_ignores_undecorated():
    tools = server._parse_registered_tools(
        "async def bare(ctx):\n    pass\n"
    )
    assert tools == []


def test_parse_multiple_tools():
    src = (
        "from miragen import register\n\n"
        "@register\nasync def alpha(ctx): pass\n\n"
        "@register\nasync def beta(ctx): pass\n"
    )
    tools = server._parse_registered_tools(src)
    assert [t["name"] for t in tools] == ["alpha", "beta"]


def test_parse_invalid_syntax():
    assert server._parse_registered_tools("def (broken:") == []


# ── _find_function_span ───────────────────────────────────────────────────────

def test_find_span_second_function():
    src = (
        "from miragen import register\n\n"
        "@register\nasync def first(ctx):\n    return 1\n\n"
        "@register\nasync def second(ctx):\n    return 2\n"
    )
    span = server._find_function_span(src, "second")
    assert span is not None
    chunk = "\n".join(src.splitlines()[span[0]:span[1]])
    assert "second" in chunk
    assert "first" not in chunk


def test_find_span_missing():
    assert server._find_function_span("async def foo(): pass\n", "bar") is None


def test_find_span_invalid_syntax():
    assert server._find_function_span("def (broken:", "foo") is None


# ── read_agent_file / write_agent_file / edit_agent_file ──────────────────────

def test_read_agent_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "data.txt").write_text("hello")
    assert server.read_agent_file("a", "data.txt") == "hello"


def test_read_agent_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / "a").mkdir(parents=True)
    assert server.read_agent_file("a", "nope.txt").startswith("ERROR:")


def test_read_agent_file_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    assert server.read_agent_file("a", "../b/secret.txt").startswith("ERROR:")


def test_write_agent_file_creates_parents(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / "a").mkdir(parents=True)
    server.write_agent_file("a", "sub/out.txt", "content")
    assert (tmp_path / "agents" / "a" / "sub" / "out.txt").read_text() == "content"


def test_edit_agent_file_success(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "f.txt").write_text("foo bar baz")
    assert server.edit_agent_file("a", "f.txt", "bar", "qux") == "Edited f.txt"
    assert (d / "f.txt").read_text() == "foo qux baz"


def test_edit_agent_file_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "f.txt").write_text("hello")
    assert server.edit_agent_file("a", "f.txt", "nope", "x").startswith("ERROR:")


def test_edit_agent_file_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "f.txt").write_text("x x x")
    result = server.edit_agent_file("a", "f.txt", "x", "y")
    assert "3 times" in result


def test_edit_agent_file_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / "a").mkdir(parents=True)
    assert server.edit_agent_file("a", "missing.txt", "x", "y").startswith("ERROR:")


# ── set_retrigger arg validation ──────────────────────────────────────────────

def test_retrigger_neither_arg():
    assert server.set_retrigger("agent", "do something").startswith("ERROR:")


def test_retrigger_both_args():
    assert server.set_retrigger("agent", "do something", delay_seconds=10, at="2030-01-01T00:00:00").startswith("ERROR:")


def test_retrigger_delay(monkeypatch):
    monkeypatch.setattr(server, "_scheduler", pytest.importorskip("unittest.mock").MagicMock())
    result = server.set_retrigger("agent", "do something", delay_seconds=60)
    assert "agent" in result
    assert result.startswith("Retrigger scheduled")


# ── register_tool rollback ────────────────────────────────────────────────────

def test_register_tool_rollback_on_restart_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(server, "restart_agent", lambda name: "ERROR: container not found")

    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    orig_tools = "from miragen import register\n\n# Tools for a\n"
    orig_yaml = "name: a\nmode: autonomous\ntools: []\n"
    (d / "tools.py").write_text(orig_tools)
    (d / "agent.yaml").write_text(orig_yaml)

    result = server.register_tool("a", "new_tool", "@register\nasync def new_tool(ctx): pass\n")

    assert result.startswith("ERROR:")
    assert (d / "tools.py").read_text() == orig_tools
    assert (d / "agent.yaml").read_text() == orig_yaml


def test_register_tool_success(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(server, "restart_agent", lambda name: f"Agent {name} restarted.")

    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "tools.py").write_text("from miragen import register\n\n# Tools for a\n")
    (d / "agent.yaml").write_text("name: a\nmode: autonomous\ntools: []\n")

    result = server.register_tool("a", "my_tool", "@register\nasync def my_tool(ctx): pass\n")
    assert "registered" in result
    assert "my_tool" in (d / "tools.py").read_text()
    yaml_data = server._read_yaml(d / "agent.yaml")
    assert "my_tool" in yaml_data["tools"]


# ── agent name validation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["../escape", "UPPER", "has space", "", ".hidden", "a/b"])
def test_invalid_agent_names_rejected(bad):
    assert server._check_agent_name(bad) is not None


@pytest.mark.parametrize("good", ["a", "morning-briefing", "agent_2", "0abc"])
def test_valid_agent_names_accepted(good):
    assert server._check_agent_name(good) is None


def test_read_agent_file_agent_name_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "secret.txt").write_text("top secret")
    result = server.read_agent_file("..", "secret.txt")
    assert result.startswith("ERROR:")


def test_get_agent_invalid_name():
    result = server.get_agent("../../etc")
    assert "error" in result
    assert result["error"].startswith("ERROR:")


def test_create_agent_invalid_name():
    assert server.create_agent("Bad Name", "name: x").startswith("ERROR:")


# ── register_tool source validation ──────────────────────────────────────────

def test_register_tool_rejects_invalid_syntax(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    orig = "from miragen import register\n"
    (d / "tools.py").write_text(orig)
    result = server.register_tool("a", "broken", "def (broken:")
    assert result.startswith("ERROR:")
    assert "valid Python" in result
    assert (d / "tools.py").read_text() == orig


def test_register_tool_rejects_name_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    orig = "from miragen import register\n"
    (d / "tools.py").write_text(orig)
    result = server.register_tool("a", "expected", "@register\nasync def other(ctx): pass\n")
    assert result.startswith("ERROR:")
    assert "expected" in result
    assert (d / "tools.py").read_text() == orig


def test_register_tool_accepts_named_decorator(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(server, "restart_agent", lambda name: f"Agent {name} restarted.")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "tools.py").write_text("from miragen import register\n")
    (d / "agent.yaml").write_text("name: a\ntools: []\n")
    result = server.register_tool("a", "speak", '@register("speak")\nasync def tts(ctx, text: str): pass\n')
    assert "registered" in result


# ── output truncation ─────────────────────────────────────────────────────────

def test_truncate_short_passthrough():
    assert server._truncate("abc") == "abc"


def test_truncate_long_output():
    result = server._truncate("x" * 60_000)
    assert len(result) < 60_000
    assert "TRUNCATED" in result


def test_read_agent_file_truncates(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "big.txt").write_text("y" * (server.MAX_OUTPUT_CHARS + 1000))
    result = server.read_agent_file("a", "big.txt")
    assert "TRUNCATED" in result


# ── set_retrigger datetime validation ─────────────────────────────────────────

def test_retrigger_invalid_iso():
    result = server.set_retrigger("agent", "go", at="not-a-date")
    assert result.startswith("ERROR:")
    assert "ISO 8601" in result


def test_retrigger_past_datetime():
    result = server.set_retrigger("agent", "go", at="2000-01-01T00:00:00+00:00")
    assert result.startswith("ERROR:")
    assert "past" in result


# ── delete_tool ───────────────────────────────────────────────────────────────

def test_delete_tool_removes_function_and_yaml_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(server, "restart_agent", lambda name: f"Agent {name} restarted.")

    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "tools.py").write_text(
        "from miragen import register\n\n"
        "@register\nasync def keeper(ctx): pass\n\n"
        "@register\nasync def goner(ctx): pass\n"
    )
    (d / "agent.yaml").write_text("name: a\ntools:\n- keeper\n- goner\n")

    result = server.delete_tool("a", "goner")
    assert "restarted" in result

    src = (d / "tools.py").read_text()
    assert "goner" not in src
    assert "keeper" in src

    tools_list = server._read_yaml(d / "agent.yaml")["tools"]
    assert "goner" not in tools_list
    assert "keeper" in tools_list


def test_delete_tool_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "tools.py").write_text("from miragen import register\n")
    result = server.delete_tool("a", "ghost")
    assert result.startswith("ERROR:")


# ── edit_tool ─────────────────────────────────────────────────────────────────

def test_edit_tool_tool_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    original = "from miragen import register\n\n@register\nasync def keeper(ctx):\n    return 1\n"
    (d / "tools.py").write_text(original)

    result = server.edit_tool("a", "ghost", "return 1", "return 2")

    assert result.startswith("ERROR:")
    assert "not found" in result
    assert (d / "tools.py").read_text() == original


def test_edit_tool_old_str_outside_named_span(tmp_path, monkeypatch):
    """old_str is unique in the whole file but lives inside a *different*
    function than the one named by tool_name — must not edit the wrong tool."""
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    original = (
        "from miragen import register\n\n"
        "@register\nasync def alpha(ctx):\n    return 'alpha-marker'\n\n"
        "@register\nasync def beta(ctx):\n    return 'beta-marker'\n"
    )
    (d / "tools.py").write_text(original)

    result = server.edit_tool("a", "beta", "alpha-marker", "hacked")

    assert result.startswith("ERROR:")
    assert (d / "tools.py").read_text() == original


def test_edit_tool_no_match_within_span(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    original = "from miragen import register\n\n@register\nasync def solo(ctx):\n    return 1\n"
    (d / "tools.py").write_text(original)

    result = server.edit_tool("a", "solo", "nope", "x")

    assert result.startswith("ERROR:")
    assert (d / "tools.py").read_text() == original


def test_edit_tool_ambiguous_within_span(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    original = (
        "from miragen import register\n\n"
        "@register\nasync def dup(ctx):\n    x = 1\n    x = 1\n    return x\n"
    )
    (d / "tools.py").write_text(original)

    result = server.edit_tool("a", "dup", "x = 1", "x = 2")

    assert result.startswith("ERROR:")
    assert "2 times" in result
    assert (d / "tools.py").read_text() == original


def test_edit_tool_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(server, "restart_agent", lambda name: f"Agent {name} restarted.")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "tools.py").write_text(
        "from miragen import register\n\n"
        "@register\nasync def alpha(ctx):\n    return 'alpha-marker'\n\n"
        "@register\nasync def beta(ctx):\n    return 'beta-marker'\n"
    )

    result = server.edit_tool("a", "beta", "beta-marker", "beta-updated")

    assert result.startswith("Tool 'beta' edited")
    assert "restarted" in result
    src = (d / "tools.py").read_text()
    assert "beta-updated" in src
    assert "beta-marker" not in src
    assert "alpha-marker" in src  # other function untouched


def test_edit_tool_restart_failure_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(server, "restart_agent", lambda name: "ERROR: container not found")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "tools.py").write_text(
        "from miragen import register\n\n@register\nasync def solo(ctx):\n    return 1\n"
    )

    result = server.edit_tool("a", "solo", "return 1", "return 2")

    assert result.startswith("Tool edited but restart failed")
    assert "return 2" in (d / "tools.py").read_text()


# ── write_agent_file / edit_agent_file agent.yaml note ────────────────────────

def test_write_agent_file_agent_yaml_adds_note(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    result = server.write_agent_file("a", "agent.yaml", "name: a\n")
    assert "miragen_update_agent_config" in result


def test_write_agent_file_other_path_no_note(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    result = server.write_agent_file("a", "notes.txt", "hi")
    assert "miragen_update_agent_config" not in result


def test_edit_agent_file_agent_yaml_adds_note(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text("name: a\nmode: autonomous\n")
    result = server.edit_agent_file("a", "agent.yaml", "autonomous", "reactive")
    assert "miragen_update_agent_config" in result


# ── update_agent_config ───────────────────────────────────────────────────────

def _fake_run(returncode, output=""):
    def run(*a, **kw):
        return type("Result", (), {"returncode": returncode, "stdout": output, "stderr": ""})()
    return run


def test_update_agent_config_agent_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    result = server.update_agent_config("missing", "name: missing\n")
    assert result.startswith("ERROR:")
    assert "miragen_list_agents" in result
    assert "miragen_create_agent" in result


def test_update_agent_config_invalid_yaml_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(server.subprocess, "run", _fake_run(1, "schema error: bad field"))
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    orig_yaml = "name: a\nmode: autonomous\ntools: []\n"
    (d / "agent.yaml").write_text(orig_yaml)

    result = server.update_agent_config("a", "name: a\nmode: broken-mode\n")

    assert result.startswith("ERROR: validation failed:")
    assert "untouched" in result
    assert (d / "agent.yaml").read_text() == orig_yaml
    assert not (d / "agent.yaml.candidate").exists()


def test_update_agent_config_name_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(server.subprocess, "run", _fake_run(0))
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    orig_yaml = "name: a\nmode: autonomous\ntools: []\n"
    (d / "agent.yaml").write_text(orig_yaml)

    result = server.update_agent_config("a", "name: b\nmode: autonomous\n")

    assert result.startswith("ERROR:")
    assert "'a'" in result
    assert (d / "agent.yaml").read_text() == orig_yaml
    assert not (d / "agent.yaml.candidate").exists()


def test_update_agent_config_restart_failure_restores(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(server.subprocess, "run", _fake_run(0))
    monkeypatch.setattr(server, "restart_agent", lambda name: "ERROR: container not found")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    orig_yaml = "name: a\nmode: autonomous\ntools: []\n"
    (d / "agent.yaml").write_text(orig_yaml)

    result = server.update_agent_config("a", "name: a\nmode: reactive\ntools: []\n")

    assert result.startswith("ERROR:")
    assert "restart failed" in result
    assert "previous config restored" in result
    assert (d / "agent.yaml").read_text() == orig_yaml
    assert not (d / "agent.yaml.candidate").exists()


def test_update_agent_config_success_diff_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(server.subprocess, "run", _fake_run(0))
    monkeypatch.setattr(server, "restart_agent", lambda name: f"Agent {name} restarted.")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text("name: a\nmode: autonomous\ntools: []\n")

    result = server.update_agent_config("a", "name: a\nmode: reactive\ntools: []\nextra: 1\n")

    assert result.startswith("Config updated and a restarted.")
    assert "mode" in result
    assert "extra" in result
    assert "tools" not in result.split("Diff summary:")[1]
    assert (d / "agent.yaml").read_text() == "name: a\nmode: reactive\ntools: []\nextra: 1\n"
    assert not (d / "agent.yaml.candidate").exists()


# ── resources: miragen://agents ───────────────────────────────────────────────

def test_agents_resource_matches_list_agents(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text("name: a\nmode: autonomous\nspec:\n  model: anthropic/claude\n")
    assert server.agents_resource() == server.list_agents()


# ── resources: miragen://agents/{name}/agent.yaml ─────────────────────────────

def test_agent_yaml_resource_matches_file_content(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text("name: a\nmode: autonomous\n")
    assert server.agent_yaml_resource("a") == server.read_agent_file("a", "agent.yaml")
    assert server.agent_yaml_resource("a") == server.get_agent("a")["yaml"]


def test_agent_yaml_resource_invalid_name_raises():
    with pytest.raises(ValueError):
        server.agent_yaml_resource("../escape")


def test_agent_yaml_resource_missing_agent_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    with pytest.raises(ValueError):
        server.agent_yaml_resource("ghost")


def test_agent_yaml_resource_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / "a").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        server.agent_yaml_resource("a")


# ── resources: miragen://agents/{name}/tools.py ───────────────────────────────

def test_agent_tools_resource_matches_file_content(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    d = tmp_path / "agents" / "a"
    d.mkdir(parents=True)
    (d / "tools.py").write_text("from miragen import register\n\n# Tools for a\n")
    assert server.agent_tools_resource("a") == server.read_agent_file("a", "tools.py")


def test_agent_tools_resource_invalid_name_raises():
    with pytest.raises(ValueError):
        server.agent_tools_resource("Bad Name")


def test_agent_tools_resource_missing_agent_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    with pytest.raises(ValueError):
        server.agent_tools_resource("ghost")


def test_agent_tools_resource_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / "a").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        server.agent_tools_resource("a")


# ── resource: miragen://docs/readme ───────────────────────────────────────────

def test_readme_resource_returns_fetched_content(monkeypatch):
    monkeypatch.setattr(server, "_readme_cache", None)
    monkeypatch.setattr(server, "get_miragen_readme", lambda: "# Miragen\nfetched content")
    assert server.readme_resource() == "# Miragen\nfetched content"


def test_readme_resource_caches_after_success(monkeypatch):
    monkeypatch.setattr(server, "_readme_cache", None)
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return f"content #{calls['n']}"

    monkeypatch.setattr(server, "get_miragen_readme", fake_fetch)
    first = server.readme_resource()
    second = server.readme_resource()
    assert first == second == "content #1"
    assert calls["n"] == 1


def test_readme_resource_falls_back_when_fetch_fails(monkeypatch):
    monkeypatch.setattr(server, "_readme_cache", None)
    monkeypatch.setattr(server, "get_miragen_readme", lambda: "ERROR: could not fetch README: timeout")
    result = server.readme_resource()
    assert result == server._README_FALLBACK
    assert "agent.yaml" in result


def test_readme_resource_retries_after_failure(monkeypatch):
    monkeypatch.setattr(server, "_readme_cache", None)
    calls = {"n": 0}

    def flaky_fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            return "ERROR: no network"
        return "fetched on retry"

    monkeypatch.setattr(server, "get_miragen_readme", flaky_fetch)
    assert server.readme_resource() == server._README_FALLBACK
    assert server.readme_resource() == "fetched on retry"


# ── prompt: create-agent ──────────────────────────────────────────────────────

def test_create_agent_prompt_default_mode():
    result = server.create_agent_prompt("send a daily weather summary")
    assert "send a daily weather summary" in result
    assert "autonomous" in result


def test_create_agent_prompt_explicit_mode():
    result = server.create_agent_prompt("watch a webhook", mode="reactive")
    assert "watch a webhook" in result
    assert "reactive" in result


def test_create_agent_prompt_references_real_tools():
    result = server.create_agent_prompt("do something")
    for tool_name in (
        "miragen_get_readme",
        "miragen_validate_yaml",
        "miragen_create_agent",
        "miragen_get_agent_logs",
        "miragen_register_tool",
    ):
        assert tool_name in result


# ── real fastmcp end-to-end sanity (skipped if fastmcp isn't installed) ──────

def test_resource_and_prompt_decorators_accept_real_fastmcp_kwargs():
    """server.py calls mcp.resource(...)/mcp.prompt(...) with specific kwargs (mime_type,
    name, description, uri templates with {param}). conftest fakes these as passthroughs
    for unit testing, so this checks the real library actually accepts that call shape.

    conftest.py unconditionally installs MagicMocks at sys.modules["fastmcp"] and
    sys.modules["starlette*"] (server.py must import successfully even where the real
    packages aren't installed), so a plain `pytest.importorskip("fastmcp")` would just find
    the stub and never exercise the real library. Check the real distribution via
    importlib.metadata instead, and swap the stubs out of sys.modules -- fastmcp pulls in
    starlette transitively -- for the duration of this one test.
    """
    import importlib
    import importlib.metadata
    import sys

    try:
        importlib.metadata.version("fastmcp")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("real fastmcp package is not installed (only the conftest stub is present)")

    def _is_stubbed_dep(n: str) -> bool:
        return n == "fastmcp" or n.startswith(("fastmcp.", "starlette", "mcp", "uvicorn"))

    saved = {n: sys.modules.pop(n) for n in list(sys.modules) if _is_stubbed_dep(n)}
    try:
        real_fastmcp = importlib.import_module("fastmcp")
        real_mcp = real_fastmcp.FastMCP("test-server")

        template = real_mcp.resource(
            "miragen://agents/{name}/agent.yaml",
            name="Agent Profile",
            description="Raw agent.yaml contents for one agent.",
            mime_type="text/yaml",
        )(lambda name: f"name: {name}\n")
        assert template.name == "Agent Profile"

        prompt = real_mcp.prompt(name="create-agent")(
            lambda purpose, mode="autonomous": f"{purpose} {mode}"
        )
        assert prompt.name == "create-agent"
    finally:
        for n in [n for n in sys.modules if _is_stubbed_dep(n)]:
            del sys.modules[n]
        sys.modules.update(saved)
