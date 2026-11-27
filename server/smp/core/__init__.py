# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""SMP core: shared domain model, auth, RBAC, audit, events and the module registry.

Modules should import from here, e.g.::

    from smp.core.context import Ctx, require
    from smp.core.models import Device, Room
    from smp.core.module import Module, Permission, UiRoute, EventSubject
"""
