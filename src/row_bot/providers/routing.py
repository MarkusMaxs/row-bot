from __future__ import annotations

from row_bot.providers.config import load_provider_config

# Routing execution is not enabled yet, so the profile that describes today's
# behaviour ("use the selected default model") is the one background calls read.
DEFAULT_ROUTE_ID = "balanced"


def list_routing_profiles() -> list[dict]:
    return [route for route in load_provider_config().get("routes", []) if isinstance(route, dict)]


def get_routing_profile(route_id: str) -> dict | None:
    for route in list_routing_profiles():
        if route.get("id") == route_id:
            return route
    return None


def task_route_model(purpose: str, route_id: str = DEFAULT_ROUTE_ID) -> str:
    """The model a routing profile names for one background purpose, or "".

    ``task_routes`` maps a purpose (``extraction``, ``dream``, ``verifier`` ...)
    to a provider-qualified model ref. Nothing sets it today and there is no
    setting for it (D1): every route ships with ``{}``, so background calls keep
    their own model rule. It is the place a cheaper model for one purpose would
    be named later, without redesigning the background resolver.
    """
    route = get_routing_profile(route_id)
    if not route or route.get("enabled") is False:
        return ""
    routes = route.get("task_routes")
    if not isinstance(routes, dict):
        return ""
    value = routes.get(str(purpose or ""))
    return value.strip() if isinstance(value, str) else ""
