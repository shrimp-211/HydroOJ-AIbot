"""模块注册表与统一入口。"""
import importlib
import sys

import pytest

import hydro
from hydroai.modules import describe, get_spec, list_specs


def test_registry_declares_four_modules_with_valid_dependencies():
    names = [s.name for s in list_specs()]
    assert names == ["ojkit", "solver", "insight", "assistant"]
    for spec in list_specs():
        assert set(spec.requires) <= set(names) - {spec.name}
    assert get_spec("assistant").requires == ("ojkit", "insight")


def test_unknown_module_is_reported():
    with pytest.raises(KeyError):
        get_spec("nope")
    assert hydro.main(["nope"]) == 2


def test_hydro_lists_modules(capsys):
    assert hydro.main([]) == 0
    out = capsys.readouterr().out
    assert all(m["name"] in out for m in describe())


@pytest.mark.parametrize("name", ["ojkit", "solver"])
def test_entry_points_are_importable(name):
    module_path, func = get_spec(name).entry.split(":")
    assert callable(getattr(importlib.import_module(module_path), func))


def test_solver_cli_passes_args_through_and_restores_argv(monkeypatch):
    from hydroai.solve import cli
    seen = {}

    def fake_main():
        seen["argv"] = list(sys.argv)
        return 0

    fake_module = type("M", (), {"main": staticmethod(fake_main)})
    monkeypatch.setattr(cli.importlib, "import_module", lambda path: fake_module)
    before = list(sys.argv)
    assert cli.main(["solve", "https://x/p/1", "--dry-run"]) == 0
    assert seen["argv"][1:] == ["https://x/p/1", "--dry-run"] and sys.argv == before
    assert cli.main(["bogus"]) == 2
