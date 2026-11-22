"""Compile-time module list. To add a module: create smp/modules/<name>/, then add it
here. See docs/writing-a-module.md."""

from smp.core.core_module import CoreModule
from smp.core.module import Module, ModuleRegistry
from smp.modules.dns import DnsModule
from smp.modules.shortlinks import ShortlinksModule


def all_modules() -> list[Module]:
    return [
        CoreModule(),
        ShortlinksModule(),
        DnsModule(),
        # AgentModule(), SoftwareModule(), ClassroomModule(), IdentityModule(),
    ]


def build_registry(enabled: set[str] | None = None) -> ModuleRegistry:
    """enabled=None means all. Core is always enabled."""
    mods = [m for m in all_modules() if enabled is None or m.name == "core" or m.name in enabled]
    return ModuleRegistry(mods)
