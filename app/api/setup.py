"""Setup wizard API endpoints."""

import threading
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.api.auth import bearer_token
from app.core.setup import (
    apply_setup_request,
    get_setup_schema,
    get_setup_status,
    load_existing_config,
    test_gateway_connection,
    validate_setup_request,
    verify_setup_token,
    auth_status,
    cloud_auth_complete,
    cloud_auth_site,
    cloud_auth_start,
    cloud_auth_use,
    docker_preflight,
    env_import_activate,
    env_import_preview,
    import_auth_file,
    import_auth_tokens,
    fleet_generate_key,
    fleet_setup_complete,
    fleet_setup_site,
    fleet_setup_start,
    scan_gateways,
    v1r_auth_file,
    v1r_key,
    v1r_register,
    v1r_registration_status,
)
from app.models.setup import (
    SetupAuthFileRequest,
    SetupAuthFileResult,
    SetupAuthTokensRequest,
    SetupAuthTokensResult,
    SetupAuthRequest,
    SetupCloudAuthCompleteRequest,
    SetupCloudAuthCompleteResult,
    SetupCloudAuthStartRequest,
    SetupCloudAuthStartResult,
    SetupCloudAuthUseRequest,
    SetupCloudSiteRequest,
    SetupCloudSiteResult,
    SetupConfigureRequest,
    SetupConfigureResult,
    SetupDiagnosticResult,
    SetupEnvImportRequest,
    SetupEnvImportResult,
    SetupFleetCompleteRequest,
    SetupFleetCompleteResult,
    SetupFleetKeyRequest,
    SetupFleetKeyResult,
    SetupFleetSiteRequest,
    SetupFleetSiteResult,
    SetupFleetStartRequest,
    SetupFleetStartResult,
    SetupGatewayInput,
    SetupKeyRequest,
    SetupReloadResult,
    SetupScanRequest,
    SetupScanResult,
    SetupStatus,
    SetupV1rAuthFileRequest,
    SetupV1rAuthFileResult,
    SetupV1rRegisterRequest,
    SetupV1rResult,
    SetupV1rStatusRequest,
    SetupValidationResult,
)

_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: Dict[str, tuple[float, int]] = {}
_RATE_LIMIT = 120
_RATE_WINDOW = 60.0


def _check_rate(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _RATE_LOCK:
        start, count = _RATE_BUCKETS.get(key, (now, 0))
        if now - start >= _RATE_WINDOW:
            start, count = now, 0
        count += 1
        _RATE_BUCKETS[key] = (start, count)
        if len(_RATE_BUCKETS) > 1024:
            for old_key, (old_start, _) in list(_RATE_BUCKETS.items()):
                if now - old_start >= _RATE_WINDOW:
                    _RATE_BUCKETS.pop(old_key, None)
    if count > _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Setup rate limit exceeded")


async def require_setup_auth(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_pw_setup_token: Optional[str] = Header(None),
) -> bool:
    _check_rate(request)
    token = x_pw_setup_token or bearer_token(authorization)
    if not verify_setup_token(token):
        raise HTTPException(status_code=401, detail="Valid setup token required")
    return True


router = APIRouter(dependencies=[Depends(require_setup_auth)])


@router.get("/status", response_model=SetupStatus)
async def status() -> SetupStatus:
    return get_setup_status()


@router.get("/schema")
async def schema() -> Dict[str, Any]:
    return get_setup_schema()


@router.post("/validate", response_model=SetupValidationResult)
async def validate(request: SetupConfigureRequest) -> SetupValidationResult:
    result, _, _ = validate_setup_request(request, load_existing_config())
    return result


@router.post("/auth/status")
async def auth_status_endpoint(request: SetupAuthRequest):
    return auth_status(request.mode, request.authpath)


@router.post("/auth/tokens", response_model=SetupAuthTokensResult)
async def auth_tokens_endpoint(request: SetupAuthTokensRequest):
    try:
        return await run_in_threadpool(import_auth_tokens, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"unable to save the pasted tokens ({type(exc).__name__})",
        ) from exc


@router.post("/auth/file", response_model=SetupAuthFileResult)
async def auth_file_endpoint(request: SetupAuthFileRequest):
    try:
        return import_auth_file(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"auth file import failed ({type(exc).__name__})",
        ) from exc


@router.post("/auth/cloud/start", response_model=SetupCloudAuthStartResult)
async def cloud_auth_start_endpoint(request: SetupCloudAuthStartRequest):
    try:
        return cloud_auth_start(request.email, request.authpath, request.region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/auth/cloud/complete", response_model=SetupCloudAuthCompleteResult)
async def cloud_auth_complete_endpoint(request: SetupCloudAuthCompleteRequest):
    try:
        return await run_in_threadpool(cloud_auth_complete, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Tesla authentication failed ({type(exc).__name__})",
        ) from exc


@router.post("/auth/cloud/use", response_model=SetupCloudAuthCompleteResult)
async def cloud_auth_use_endpoint(request: SetupCloudAuthUseRequest):
    try:
        return await run_in_threadpool(cloud_auth_use, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Tesla Cloud auth check failed ({type(exc).__name__})",
        ) from exc


@router.post("/auth/cloud/site", response_model=SetupCloudSiteResult)
async def cloud_auth_site_endpoint(request: SetupCloudSiteRequest):
    try:
        return await run_in_threadpool(
            cloud_auth_site, request.email, request.authpath, request.siteid
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/scan", response_model=SetupScanResult)
async def scan_endpoint(request: SetupScanRequest):
    try:
        return await run_in_threadpool(scan_gateways, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"gateway scan failed ({type(exc).__name__})",
        ) from exc


@router.get("/docker/preflight")
async def docker_preflight_endpoint():
    return docker_preflight()


@router.get("/import-env")
async def import_env_endpoint():
    return env_import_preview()


@router.post("/import-env", response_model=SetupEnvImportResult)
async def import_env_activate_endpoint(request: SetupEnvImportRequest):
    try:
        return env_import_activate(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/v1r/key")
async def v1r_key_endpoint(request: SetupKeyRequest):
    try:
        return await run_in_threadpool(v1r_key, request.authpath)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"key generation failed: {type(exc).__name__}"
        ) from exc


@router.post("/v1r/auth-file", response_model=SetupV1rAuthFileResult)
async def v1r_auth_file_endpoint(request: SetupV1rAuthFileRequest):
    try:
        return v1r_auth_file(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"auth file import failed ({type(exc).__name__})",
        ) from exc


@router.post("/v1r/register", response_model=SetupV1rResult)
async def v1r_register_endpoint(request: SetupV1rRegisterRequest):
    try:
        return await run_in_threadpool(v1r_register, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"v1r key registration failed ({type(exc).__name__})",
        ) from exc


@router.post("/v1r/register/status", response_model=SetupV1rResult)
async def v1r_register_status_endpoint(request: SetupV1rStatusRequest):
    try:
        return await run_in_threadpool(v1r_registration_status, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"v1r key status check failed ({type(exc).__name__})",
        ) from exc


@router.post("/fleetapi/key", response_model=SetupFleetKeyResult)
async def fleetapi_key_endpoint(request: SetupFleetKeyRequest):
    try:
        return fleet_generate_key(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"FleetAPI key generation failed ({type(exc).__name__})",
        ) from exc


@router.post("/fleetapi/start", response_model=SetupFleetStartResult)
async def fleetapi_start_endpoint(request: SetupFleetStartRequest):
    try:
        return await run_in_threadpool(fleet_setup_start, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"FleetAPI setup failed ({type(exc).__name__})",
        ) from exc


@router.post("/fleetapi/complete", response_model=SetupFleetCompleteResult)
async def fleetapi_complete_endpoint(request: SetupFleetCompleteRequest):
    try:
        return await run_in_threadpool(fleet_setup_complete, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"FleetAPI login failed ({type(exc).__name__})",
        ) from exc


@router.post("/fleetapi/site", response_model=SetupFleetSiteResult)
async def fleetapi_site_endpoint(request: SetupFleetSiteRequest):
    try:
        return await run_in_threadpool(fleet_setup_site, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"FleetAPI site selection failed ({type(exc).__name__})",
        ) from exc


@router.post("/test-connection", response_model=SetupDiagnosticResult)
async def test_connection(gateway: SetupGatewayInput) -> SetupDiagnosticResult:
    try:
        existing = load_existing_config()
        match = next(
            (
                item
                for item in existing.get("gateways", [])
                if isinstance(item, dict) and item.get("id") == gateway.id
            ),
            {},
        )
        return await run_in_threadpool(test_gateway_connection, gateway, match)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/configure", response_model=SetupConfigureResult)
async def configure(request: SetupConfigureRequest) -> SetupConfigureResult:
    try:
        return apply_setup_request(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reload", response_model=SetupReloadResult)
async def reload() -> SetupReloadResult:
    return SetupReloadResult(
        restart_required=True,
        message="Restart the server/container to activate the saved configuration.",
    )
