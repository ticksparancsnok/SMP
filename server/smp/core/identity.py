"""Identity provider interface.

Login looks up the user in our DB first (users are always mirrored locally so that
roles, groups and audit work the same for everyone), then asks the provider named in
User.auth_source to verify the password. The identity module will add providers such
as "ldap" (bind against AD/lldap), and sync jobs for Google Workspace / Entra that
create users with auth_source="google" etc.
"""

from typing import Protocol

from smp.core.models import AuthSource, User
from smp.core.security import verify_password_async


class IdentityProvider(Protocol):
    source: str

    async def verify_password(self, user: User, password: str) -> bool: ...


class LocalIdentityProvider:
    source = AuthSource.LOCAL

    async def verify_password(self, user: User, password: str) -> bool:
        return await verify_password_async(user.password_hash, password)
