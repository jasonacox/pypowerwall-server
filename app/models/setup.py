"""Pydantic models for the graphical setup wizard."""

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, SecretStr, field_validator

SetupGatewayMode = Literal[
    "tedapi",
    "tedapi_v1r",
    "tedapi_bearer",
    "local",
    "basic_lan",
    "hybrid_tedapi",
    "cloud",
    "fleetapi",
    "hybrid",
]


class SetupGatewayInput(BaseModel):
    """One gateway as entered in the setup wizard."""

    id: str
    name: Optional[str] = None
    mode: SetupGatewayMode
    host: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    gw_pwd: Optional[SecretStr] = None
    password: Optional[SecretStr] = None
    rsa_key_path: Optional[str] = None
    wifi_host: Optional[str] = None
    email: Optional[str] = None
    authpath: Optional[str] = None
    timezone: str = "America/Los_Angeles"
    type: str = "powerwall"
    siteid: Optional[str] = None
    tedapi_auth_mode: Optional[str] = None
    tedapi_api_version: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("Gateway ID is required")
        if len(text) > 64:
            raise ValueError("Gateway ID must be at most 64 characters")
        if not all(char.isalnum() or char in "-_" for char in text):
            raise ValueError("Gateway ID may contain only letters, digits, '-' and '_'")
        return text

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        text = value.strip().lower()
        if text not in {"powerwall", "inverter"}:
            raise ValueError("Gateway type must be 'powerwall' or 'inverter'")
        return text

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if "@" not in text or any(character.isspace() for character in text):
            raise ValueError("a valid Tesla email is required")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
            raise ValueError("a valid Tesla email is required")
        return text

    @field_validator("siteid", mode="before")
    @classmethod
    def _normalize_siteid(cls, value):
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class SetupGatewaySummary(BaseModel):
    """Masked gateway state returned by the setup status API."""

    id: str
    name: str
    host: Optional[str] = None
    port: Optional[int] = None
    type: str = "powerwall"
    mode: Optional[str] = None
    online: bool = False
    last_error: Optional[str] = None
    siteid_configured: bool = False
    rsa_key_configured: bool = False
    authpath_configured: bool = False


class SetupStatus(BaseModel):
    """Current setup state without credentials or private material."""

    state: str
    config_source: str
    config_path: Optional[str] = None
    config_writable: bool = False
    config_revision: Optional[str] = None
    config_error: Optional[str] = None
    config_partial: bool = False
    warnings: List[str] = Field(default_factory=list)
    gateway_count: int = 0
    gateways: List[SetupGatewaySummary] = Field(default_factory=list)
    gateway_configs: List[Dict[str, Any]] = Field(default_factory=list)
    setup_recommended: bool = False
    can_write: bool = False
    can_test: bool = False
    restart_required: bool = False
    control_enabled: bool = False
    settings_source: Optional[str] = None
    settings_keys: List[str] = Field(default_factory=list)
    settings: Dict[str, Any] = Field(default_factory=dict)


class SetupValidationIssue(BaseModel):
    """One validation error or warning."""

    field: str
    message: str
    severity: Literal["error", "warning"] = "error"


class SetupValidationResult(BaseModel):
    valid: bool
    errors: List[SetupValidationIssue] = Field(default_factory=list)
    warnings: List[SetupValidationIssue] = Field(default_factory=list)


class SetupConfigureRequest(BaseModel):
    """Full gateway list plus optional settings snapshot."""

    gateways: List[SetupGatewayInput] = Field(default_factory=list)
    options: Dict[str, Any] = Field(default_factory=dict)
    expected_revision: Optional[str] = None


class SetupConfigureResult(BaseModel):
    saved: bool
    restart_required: bool = True
    config_path: str
    backup_path: Optional[str] = None
    revision: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


class SetupReloadResult(BaseModel):
    restart_required: bool = True
    message: str


class SetupDiagnosticStep(BaseModel):
    """One bounded network/auth check."""

    key: str
    label: str
    status: Literal[
        "pending",
        "running",
        "success",
        "warning",
        "failed",
        "skipped",
    ] = "pending"
    detail: Optional[str] = None


class SetupDiagnosticResult(BaseModel):
    ok: bool
    steps: List[SetupDiagnosticStep] = Field(default_factory=list)
    detected_mode: Optional[str] = None
    detected_type: Optional[str] = None
    firmware: Optional[str] = None
    site_name: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


class SetupAuthRequest(BaseModel):
    mode: Literal["cloud", "fleetapi"] = "cloud"
    authpath: Optional[str] = None


class SetupKeyRequest(BaseModel):
    authpath: Optional[str] = None


class SetupV1rSite(BaseModel):
    siteid: str
    name: Optional[str] = None
    gateway_din: Optional[str] = None


class SetupV1rRegisterRequest(BaseModel):
    authpath: Optional[str] = None
    email: Optional[str] = None
    siteid: Optional[str] = None
    method: Literal["owner", "fleetapi"] = "owner"


class SetupV1rStatusRequest(BaseModel):
    authpath: Optional[str] = None
    email: Optional[str] = None
    siteid: str
    method: Literal["owner", "fleetapi"] = "owner"


class SetupV1rAuthFileRequest(BaseModel):
    authpath: Optional[str] = None
    content: str
    filename: Optional[str] = None


class SetupV1rAuthFileResult(BaseModel):
    saved: bool
    authpath: str
    auth_file: str
    email: Optional[str] = None
    accounts: int = 0
    message: str


class SetupV1rResult(BaseModel):
    state: Optional[int] = None
    state_name: Optional[str] = None
    verified: bool = False
    pending: bool = False
    timed_out: bool = False
    removed: bool = False
    needs_site: bool = False
    sites: List[SetupV1rSite] = Field(default_factory=list)
    siteid: Optional[str] = None
    email: Optional[str] = None
    private_key_path: str
    public_key_path: Optional[str] = None
    fingerprint: Optional[str] = None
    proof_hint: Optional[str] = None
    message: str


class SetupFleetKeyRequest(BaseModel):
    authpath: Optional[str] = None


class SetupFleetKeyResult(BaseModel):
    created: bool
    private_key_path: str
    public_key_path: str
    public_key_pem: str
    message: str


class SetupFleetStartRequest(BaseModel):
    authpath: Optional[str] = None
    client_id: str
    client_secret: SecretStr
    domain: str
    redirect_uri: Optional[str] = None
    region: Literal["na", "eu", "cn"] = "na"


class SetupFleetStartResult(BaseModel):
    auth_url: str
    expires_in: int
    public_key_url: str
    config_file: str
    message: str


class SetupFleetCompleteRequest(BaseModel):
    authpath: Optional[str] = None
    callback_url: str


class SetupFleetSite(BaseModel):
    siteid: str
    name: Optional[str] = None
    resource_type: Optional[str] = None


class SetupFleetCompleteResult(BaseModel):
    saved: bool
    authpath: str
    config_file: str
    sites: List[SetupFleetSite] = Field(default_factory=list)
    siteid: Optional[str] = None
    needs_site: bool = False
    message: str


class SetupFleetSiteRequest(BaseModel):
    authpath: Optional[str] = None
    siteid: str


class SetupFleetSiteResult(BaseModel):
    saved: bool
    siteid: str
    config_file: str
    message: str


class SetupCloudAuthStartRequest(BaseModel):
    email: str
    authpath: str
    region: Literal["us", "cn"] = "us"


class SetupCloudAuthUseRequest(BaseModel):
    authpath: str
    email: Optional[str] = None


class SetupCloudAuthStartResult(BaseModel):
    auth_url: str
    expires_in: int


class SetupCloudAuthCompleteRequest(BaseModel):
    callback_url: str
    siteid: Optional[str] = None


class SetupCloudAuthCompleteResult(BaseModel):
    saved: bool
    email: str
    authpath: str
    sites: List[Dict[str, Any]] = Field(default_factory=list)
    siteid: Optional[str] = None
    needs_site: bool = False
    notes: List[str] = Field(default_factory=list)


class SetupCloudSiteRequest(BaseModel):
    email: str
    authpath: str
    siteid: str


class SetupCloudSiteResult(BaseModel):
    saved: bool
    siteid: str
    sitefile: str


class SetupScanRequest(BaseModel):
    cidr: Optional[str] = None


class SetupScanGateway(BaseModel):
    ip: str
    din: Optional[str] = None
    firmware: Optional[str] = None


class SetupScanResult(BaseModel):
    ok: bool
    network: Optional[str] = None
    gateways: List[SetupScanGateway] = Field(default_factory=list)
    message: str


class SetupAuthFileRequest(BaseModel):
    mode: Literal["cloud", "fleetapi"]
    authpath: Optional[str] = None
    content: str
    siteid: Optional[str] = None


class SetupAuthFileResult(BaseModel):
    saved: bool
    authpath: str
    files: Dict[str, str] = Field(default_factory=dict)
    email: Optional[str] = None
    siteid: Optional[str] = None
    message: str


class SetupEnvImportRequest(BaseModel):
    expected_revision: Optional[str] = None


class SetupEnvImportResult(BaseModel):
    saved: bool
    config_path: str
    backup_path: Optional[str] = None
    revision: str
    source: Optional[str] = None
    requires_removal: bool = False
    restart_required: bool = True
    notes: List[str] = Field(default_factory=list)


class SetupAuthTokensRequest(BaseModel):
    mode: Literal["cloud"] = "cloud"
    email: Optional[str] = None
    authpath: Optional[str] = None
    refresh_token: SecretStr
    access_token: Optional[SecretStr] = None
    siteid: Optional[str] = None


class SetupAuthTokensResult(BaseModel):
    saved: bool
    authpath: str
    auth_file: str
    email: str
    siteid: Optional[str] = None
    site_file: Optional[str] = None
    message: str
