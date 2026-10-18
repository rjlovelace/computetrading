import os

from ctrade.env import find_env_file, load_env, parse_env


def test_parse_env_handles_comments_quotes_and_export():
    text = (
        "# db\nDATABASE_URL=sqlite:///x.db\n\n"
        'export ANTHROPIC_API_KEY="sk-ant-1"\nORNN=\'a=b\'\n'
    )
    assert parse_env(text) == {
        "DATABASE_URL": "sqlite:///x.db",
        "ANTHROPIC_API_KEY": "sk-ant-1",
        "ORNN": "a=b",
    }


def test_load_env_finds_parent_file_and_keeps_existing_values(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CTRADE_TEST_A=from-file\nCTRADE_TEST_B=from-file\n")
    sub = tmp_path / "app"
    sub.mkdir()
    monkeypatch.chdir(sub)
    monkeypatch.setenv("CTRADE_TEST_A", "already-set")
    monkeypatch.delenv("CTRADE_TEST_B", raising=False)
    assert find_env_file() == tmp_path / ".env"
    load_env()
    assert os.environ["CTRADE_TEST_A"] == "already-set"
    assert os.environ["CTRADE_TEST_B"] == "from-file"
