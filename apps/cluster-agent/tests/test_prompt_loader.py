"""Prompt loader — Jinja2-based with {% include %} from _shared/."""
from cluster_agent.prompts.loader import load_prompt


def test_load_prompt_returns_rendered_string():
    """load_prompt('test_simple') reads prompts/test_simple.md and returns
    its content with no template substitution since the fixture has none."""
    text = load_prompt("test_simple")
    assert text.strip() == "This is the simple test prompt."


def test_load_prompt_resolves_shared_include():
    """A prompt using {% include '_shared/test_include.md' %} resolves the
    include relative to the prompts/ directory root."""
    text = load_prompt("test_with_include")
    assert "shared content from include" in text
    assert "main prompt body" in text


def test_load_prompt_unknown_name_raises():
    """Unknown prompt names raise a clear error (not silent empty string)."""
    import pytest
    with pytest.raises(FileNotFoundError, match="does-not-exist"):
        load_prompt("does-not-exist")
