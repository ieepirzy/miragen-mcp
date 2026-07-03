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
