"""Device agents: identity, join requests, heartbeats, config delivery.

The generic part every agent capability builds on. A capability module (e.g.
classroom: screen monitoring) contributes its section of the agent's config through
Module.agent_config(); this module delivers it and wakes long-polling agents when
something they depend on changes. Protocol details: crypto.py and api.py.
"""

from fastapi import APIRouter

from smp.core.module import EventSubject, Module
from smp.modules.agent.crypto import MAX_SKEW_SECONDS
from smp.modules.agent.hub import AgentHub, ReplayGuard


class AgentModule(Module):
    name = "agent"
    title = "Device agents"
    description = "Join requests, device identity keys, heartbeats and agent configuration"
    # Approving machines uses core.devices.write; agents themselves sign requests.
    events = (
        EventSubject("agent.config.changed", "Something agents' configs depend on changed (wakes heartbeats)"),
    )

    # Subjects after which agents should fetch their config again.
    WAKE_ON = ("core.policy.changed", "core.settings.changed", "core.device.*", "core.room.*", "agent.config.changed")

    def __init__(self) -> None:
        self.hub = AgentHub()
        # Twice the allowed clock skew: a signature can't be valid for longer than that.
        self.replay = ReplayGuard(ttl_seconds=2 * MAX_SKEW_SECONDS)

    def router(self) -> APIRouter:
        from smp.modules.agent.api import router

        return router

    def public_router(self) -> APIRouter:
        from smp.modules.agent.api import public

        return public  # /downloads/smp-agent.exe (download.py)

    def subscribe(self, bus) -> None:
        async def wake(_event) -> None:
            self.hub.notify()

        for subject in self.WAKE_ON:
            bus.subscribe(subject, wake)


from smp.modules.agent import models  # noqa: E402, F401  (so Alembic sees the tables)
