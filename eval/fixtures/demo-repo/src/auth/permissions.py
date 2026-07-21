ROLE_PERMISSIONS = {
    "operator": {"order:read", "order:cancel"},
    "finance": {"order:read", "order:refund"},
}


def can(user_role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(user_role, set())
