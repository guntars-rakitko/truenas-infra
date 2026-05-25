"""Prompt loader — Jinja2 + filesystem.

Prompts live at apps/cluster-agent/prompts/<name>.md, with shared
partials at apps/cluster-agent/prompts/_shared/*.md included via
Jinja {% include %}. Templates are loaded relative to the prompts/
directory root so {% include '_shared/output_schema.md' %} works
regardless of which top-level prompt did the include.

Path resolution: the prompts/ directory is at the project root
(sibling of src/), located by walking up from this file:
  loader.py                                  ← this file
  .parents[0] = .../src/cluster_agent/prompts/
  .parents[1] = .../src/cluster_agent/
  .parents[2] = .../src/
  .parents[3] = .../  (apps/cluster-agent or /app)  ← prompts/ lives here

So _ROOT = Path(__file__).resolve().parents[3] / "prompts" — same shape
in pytest (local) and in the container where code is bind-mounted at /app.
"""
from __future__ import annotations
from pathlib import Path

import jinja2


_ROOT = Path(__file__).resolve().parents[3] / "prompts"


def _env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_ROOT)),
        autoescape=False,
        keep_trailing_newline=True,
    )


def load_prompt(name: str) -> str:
    """Render the prompt at `prompts/<name>.md`.

    Raises FileNotFoundError if the named prompt doesn't exist.
    Jinja {% include %} resolves relative to the prompts/ root.
    """
    try:
        template = _env().get_template(f"{name}.md")
    except jinja2.TemplateNotFound:
        raise FileNotFoundError(f"prompt '{name}' not found at {_ROOT}/{name}.md")
    return template.render()
