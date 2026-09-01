from fastapi import APIRouter

from config.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness, plus the handful of facts that explain a confusing deploy.

    Every value is a boolean or a name — never a credential.
    """
    from marvel.links import one_tap_enabled

    settings = get_settings()
    return {
        "status": "ok",
        "marvel_credentials": settings.has_marvel_credentials,
        "one_tap_links": one_tap_enabled(),
        "telemetry": settings.otel_enabled,
    }
