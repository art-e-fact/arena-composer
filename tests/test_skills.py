"""The skills the wheel ships, and `arena-composer new`, which copies one's templates.

CI runs this against the installed wheel, so it is also the check that the files are packaged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arena_composer.cli import SKILLS, TEMPLATES, main, new

SHIPPED = sorted(path for path in SKILLS.iterdir() if path.is_dir())


def test_a_skill_is_shipped():
    assert [path.name for path in SHIPPED] == ["author-arena-spec"]


@pytest.mark.parametrize("skill", SHIPPED, ids=lambda path: path.name)
def test_a_skill_says_what_a_client_needs_to_find_it(skill: Path):
    """The Agent Skills format: frontmatter naming the directory, and a description a client
    matches requests against, at most 1024 characters."""
    _, frontmatter, body = (skill / "SKILL.md").read_text().split("---\n", 2)
    meta = yaml.safe_load(frontmatter)
    assert meta["name"] == skill.name
    assert 0 < len(meta["description"]) <= 1024
    assert body.strip() and (skill / "evaluations.md").is_file()


def test_the_template_task_includes_the_template_scene():
    task = yaml.safe_load((TEMPLATES / "tasks" / "new_task.yaml").read_text())
    scene = yaml.safe_load((TEMPLATES / "tasks" / task["external_yaml"]).read_text())
    assert {"embodiment", "background", "objects", "relations"} <= set(scene)


def test_new_lays_a_spec_out_as_a_template_project_does(tmp_path: Path):
    task = tmp_path / "environments" / "tasks" / "on.yaml"
    assert main(["new", str(task)]) == 0
    written = yaml.safe_load(task.read_text())
    # A bare `on` would be the boolean true.
    assert written["env_name"] == "on"
    assert (task.parent / written["external_yaml"]).resolve() == tmp_path / "environments" / "scenes" / "on.yaml"


def test_new_elsewhere_puts_the_scene_beside_the_task_and_overwrites_nothing(tmp_path: Path):
    scene = new(tmp_path / "pick.yaml")
    assert scene == tmp_path / "pick_scene.yaml"
    assert yaml.safe_load((tmp_path / "pick.yaml").read_text())["external_yaml"] == "pick_scene.yaml"
    with pytest.raises(FileExistsError):
        new(tmp_path / "pick.yaml")
