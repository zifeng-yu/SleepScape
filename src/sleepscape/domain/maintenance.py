"""Pure capacity thresholds shared by writers, backup tasks and status views."""

GIB = 1024**3
MIB = 1024**2
STATES = {"normal": 0, "warning": 1, "restricted": 2, "critical": 3, "unknown": 4}


def capacity_state(total: int, free: int) -> str:
    if type(total) is not int or type(free) is not int or total <= 0 or not 0 <= free <= total:
        return "unknown"
    if free * 100 < total * 5 or free < 512 * MIB:
        return "critical"
    if free * 100 < total * 15 or free < 2 * GIB:
        return "restricted"
    return "warning" if free * 100 < total * 25 else "normal"


def capacity_permission(snapshot: dict, operation: str, required_bytes: int = 0) -> str | None:
    """Return a fixed denial code; estimates reserve space before large copies."""
    if operation not in {"write", "model", "backup"}:
        raise ValueError("invalid_capacity_operation")
    if type(required_bytes) is not int or required_bytes < 0:
        raise ValueError("invalid_capacity_estimate")
    state = snapshot.get("state", "unknown")
    if state not in STATES or state == "unknown":
        return "storage_capacity_unavailable"
    if state == "critical":
        return "storage_capacity_critical"
    if operation in {"model", "backup"} and state == "restricted":
        return "storage_capacity_restricted"
    if operation == "write" and required_bytes:
        data = next((v for v in snapshot.get("volumes", []) if v.get("role") == "data"), None)
        if not data or type(data.get("free_bytes")) is not int:
            return "storage_capacity_unavailable"
        projected = capacity_state(data.get("total_bytes"), data["free_bytes"] - required_bytes)
        if projected == "unknown":
            return "storage_capacity_unavailable"
        if projected == "critical":
            return "storage_capacity_critical"
    if operation == "backup":
        data = next((v for v in snapshot.get("volumes", []) if v.get("role") == "data"), None)
        if not data:
            return "storage_capacity_unavailable"
        remaining = data["free_bytes"] - required_bytes
        if remaining < max(2 * GIB, (data["total_bytes"] * 15 + 99) // 100):
            return "backup_space_insufficient"
    return None
