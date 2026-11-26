import importlib
import pkgutil

import vingo_agent
from vingo_agent.agents.factory import load_prompt
from vingo_agent.schemas.chat import Role


def test_everyModule_imports():
    for module in pkgutil.walk_packages(vingo_agent.__path__, "vingo_agent."):
        importlib.import_module(module.name)


def test_prompts_existForBothRoles():
    assert "Vingo Buyer Assistant" in load_prompt(Role.BUYER)
    assert "Vingo Admin Assistant" in load_prompt(Role.ADMIN)
