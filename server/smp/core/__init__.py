# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""SMP core: shared domain model, auth, RBAC, audit, events and the module registry.

Modules should import from here, e.g.::

    from smp.core.context import Ctx, require
    from smp.core.models import Device, Room
    from smp.core.module import Module, Permission, UiRoute, EventSubject
"""
