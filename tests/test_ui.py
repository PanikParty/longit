from rich.console import Console

from long.agent import Agent
from long.ui import ConsoleUI


class B:
    name = "b"
    model = "a"
    def __init__(self, models): self.models = models
    def list_models(self): return self.models
    def set_model(self, m): self.model = m


def make(models, tmp_path, inputs=()):
    it = iter(inputs)
    console = Console(record=True, width=100)
    console.input = lambda *a, **k: next(it)
    ui = ConsoleUI(console)
    return ui, Agent(B(models), tmp_path, ui), console


def test_model_picker_by_number(tmp_path):
    ui, agent, console = make(["a", "b"], tmp_path, inputs=["2"])
    ui._slash("/model", agent)
    assert agent.backend.model == "b"
    assert "1. a" in console.export_text()


def test_model_by_name_and_invalid(tmp_path):
    ui, agent, console = make(["a", "b"], tmp_path)
    ui._slash("/model b", agent)
    assert agent.backend.model == "b"
    ui._slash("/model nope", agent)
    assert agent.backend.model == "b" and "not one of" in console.export_text()


def test_picker_unsupported_backend(tmp_path):
    ui, agent, console = make([], tmp_path)
    agent.backend = type("L", (), {"name": "l"})()
    ui._slash("/model", agent)
    assert "single model file" in console.export_text()
