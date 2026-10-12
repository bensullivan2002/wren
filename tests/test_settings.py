"""Tests for the saved defaults file (settings.py) and what's saved with it."""

import json

from wren.agent import Agent
from wren.commands import (
    provider_choices,
    remember_effort,
    remember_model,
    switch_provider,
)
from wren.providers.fake import FakeProvider
from wren.settings import UserDefaults
from wren.tools import Permissions, ToolRegistry, default_tools


def test_nothing_is_saved_until_something_is(tmp_path):
    user = UserDefaults(tmp_path / "sub" / "settings.json")
    assert user.get("anthropic") is None and not user.path.exists()


def test_a_default_is_kept_per_provider_and_survives_a_new_object(tmp_path):
    path = tmp_path / "sub" / "settings.json"  # the folder doesn't exist yet
    UserDefaults(path).save("anthropic", "claude-opus-5-5", "xhigh")
    UserDefaults(path).save(
        "openai", "gpt-6.1-sol", None
    )  # None: the model's own effort

    again = UserDefaults(path)
    assert again.get("anthropic") == ("claude-opus-5-5", "xhigh")
    assert again.get("openai") == ("gpt-6.1-sol", None)
    assert not path.with_name(
        "settings.json.tmp"
    ).exists()  # the side file was renamed into place


def test_saving_one_provider_leaves_everything_else_in_the_file_alone(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "something": "else",
                "defaults": {"openai": {"model": "m", "effort": "low"}},
            }
        )
    )
    UserDefaults(path).save("anthropic", "a", "high")
    data = json.loads(path.read_text())
    assert data["something"] == "else" and data["defaults"]["openai"] == {
        "model": "m",
        "effort": "low",
    }


def test_a_damaged_or_odd_file_means_nothing_saved_and_is_replaced_on_save(tmp_path):
    path = tmp_path / "settings.json"
    for text in (
        "{not json",
        "[]",
        '{"defaults": 3}',
        '{"defaults": {"anthropic": "x"}}',
        '{"defaults": {"anthropic": {"effort": "low"}}}',
    ):
        path.write_text(text)
        assert UserDefaults(path).get("anthropic") is None
    UserDefaults(path).save("anthropic", "a", None)
    assert UserDefaults(path).get("anthropic") == ("a", None)


def an_agent(tmp_path):
    tools = ToolRegistry(default_tools(), root=tmp_path, permissions=Permissions())
    return Agent(FakeProvider(), "sys", 100, tools)


def test_remember_model_saves_the_model_with_the_effort_it_now_has(tmp_path):
    user = UserDefaults(tmp_path / "settings.json")
    agent = an_agent(tmp_path)
    agent.set_effort("high")
    agent.set_model("demo-large")  # a new model: effort goes back to its own default
    assert "Saved demo-large" in remember_model(agent, user)
    assert user.get("fake") == ("demo-large", None)


def test_remember_effort_saves_only_for_the_default_model(tmp_path):
    user = UserDefaults(tmp_path / "settings.json")
    agent = an_agent(tmp_path)
    agent.set_effort("low")
    assert "Saved low as your default effort with demo" in remember_effort(agent, user)
    assert user.get("fake") == ("demo", "low")

    agent.set_model("demo-large")  # not the default model
    agent.set_effort("high")
    assert "this session only" in remember_effort(agent, user)
    assert user.get("fake") == ("demo", "low")  # unchanged


def test_nothing_is_saved_without_a_settings_file(tmp_path):
    agent = an_agent(tmp_path)
    assert remember_model(agent, None) == "" and remember_effort(agent, None) == ""


def test_a_save_that_fails_says_so_instead_of_raising(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("in the way")
    user = UserDefaults(blocked / "settings.json")  # its folder is a file
    assert "Couldn't save" in remember_model(an_agent(tmp_path), user)


def test_switching_provider_starts_on_the_saved_default_pair(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    user = UserDefaults(tmp_path / "settings.json")
    user.save("anthropic", "claude-sonnet-5-5", "low")
    agent = an_agent(tmp_path)

    assert any(
        "claude-sonnet-5-5" in label for label, _ in provider_choices(agent, user)
    )
    switch_provider(agent, "anthropic", user=user)
    assert (agent.provider.model, agent.provider.effort) == ("claude-sonnet-5-5", "low")


def test_switching_provider_without_saved_defaults_uses_wrens_own(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    agent = an_agent(tmp_path)
    switch_provider(agent, "anthropic")
    assert (agent.provider.model, agent.provider.effort) == ("claude-opus-5-5", "xhigh")
