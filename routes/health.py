from fastapi import APIRouter

from config.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness, plus the handful of facts that explain a confusing deploy.

    Every value is a boolean or a name — never a credential. `admin_key_weak`
    is surfaced precisely because a weak key is invisible until it is exploited;
    this repo is public, so an example key left in place is a published one.
    """
    from marvel.links import one_tap_enabled

    settings = get_settings()
    return {
        "status": "ok",
        "marvel_credentials": settings.has_marvel_credentials,
        "one_tap_links": one_tap_enabled(),
        "admin_key_weak": settings.admin_key_is_weak,
        "telemetry": settings.otel_enabled,
    }
