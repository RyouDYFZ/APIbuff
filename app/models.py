import time
from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from .catalog import catalog_service
from .config import DEFAULT_MODEL
from .exceptions import FreebuffException

def resolve_model_id(requested_model: str) -> str:
    """Resolves an incoming model name, slug, or alias to primary slug."""
    resolved = catalog_service.resolve_model(requested_model)
    if resolved:
        return resolved["primarySlug"]
    raise FreebuffException(
        f"Model '{requested_model}' was not found in the catalog.",
        status_code=404,
        error_type="invalid_request_error",
        error_code="model_not_found",
    )

# Pydantic models for OpenAI API compatibility
class ChatMessage(BaseModel):
    model_config = {"extra": "forbid"}
    role: Literal["system", "developer", "user", "assistant"]
    content: str

    @field_validator("content", mode="before")
    @classmethod
    def normalize_text_content(cls, value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            texts = []
            for part in value:
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "text"
                    or not isinstance(part.get("text"), str)
                    or set(part) - {"type", "text"}
                ):
                    raise ValueError("Only text content parts are supported; images, audio, and tools are not supported.")
                texts.append(part["text"])
            return "".join(texts)
        raise ValueError("Message content must be a string or an array of text parts; null content is not supported.")

class ChatCompletionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    model: str = DEFAULT_MODEL
    messages: List[ChatMessage] = Field(min_length=1)
    stream: Optional[bool] = False
    workspace: Optional[str] = None
    cwd: Optional[str] = None
    project_dir: Optional[str] = None
    session_id: Optional[str] = None
    reset_context: Optional[bool] = None

    @model_validator(mode="before")
    @classmethod
    def reject_unsupported_parameters(cls, value):
        if isinstance(value, dict):
            unsupported = sorted(set(value) - set(cls.model_fields))
            if unsupported:
                raise ValueError("Unsupported request parameter(s): " + ", ".join(unsupported))
        return value

    @field_validator("messages")
    @classmethod
    def require_current_user_request(cls, messages):
        if messages[-1].role != "user" or not messages[-1].content.strip():
            raise ValueError("The final message must be a non-empty user request; assistant prefill is not supported.")
        return messages

class ChatCompletionResponseMessage(BaseModel):
    role: str = "assistant"
    content: str
    reasoning_content: Optional[str] = None

class ChatCompletionResponseChoice(BaseModel):
    index: int = 0
    message: ChatCompletionResponseMessage
    finish_reason: str = "stop"

class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionResponseChoice]
    # The CLI adapter does not expose verified per-turn token accounting.
    usage: Optional[UsageInfo] = None

# Streaming chunk schemas
class ChatCompletionChunkDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None
    reasoning_content: Optional[str] = None

class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: ChatCompletionChunkDelta
    finish_reason: Optional[str] = None

class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChunkChoice]

# Models endpoint schemas
class ModelPricingInfo(BaseModel):
    freebucks_per_hour: Optional[float] = None
    hours_equivalent: Optional[float] = None
    is_free: Optional[bool] = None
    notice: Optional[str] = None
    off_peak: Optional[Dict[str, Any]] = None

class ModelCapabilities(BaseModel):
    context_window: int = 128000
    multimodal: bool = False
    efforts: List[str] = Field(default_factory=list)
    default_effort: Optional[str] = None
    access: str = "open"
    plan_required: Optional[bool] = None
    premium: bool = False
    data_use: str = "service"
    badges: List[str] = Field(default_factory=list)

class ModelObject(BaseModel):
    id: str
    object: str = "model"
    created: int = 1700000000
    owned_by: str = "freebuff"
    key: Optional[str] = None
    display_name: Optional[str] = None
    tagline: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    pricing: Optional[ModelPricingInfo] = None
    capabilities: Optional[ModelCapabilities] = None
    status: Optional[str] = None
    source: Optional[str] = None
    error: Optional[Any] = None

class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelObject]
    status: Optional[str] = None
    source: Optional[str] = None
    error: Optional[Any] = None

# Pricing and balance schemas
class DailyAllowanceInfo(BaseModel):
    limit_freebucks: Optional[float] = None
    limit_hours: Optional[float] = None
    spent_freebucks: Optional[float] = None
    spent_hours: Optional[float] = None
    remaining_freebucks: Optional[float] = None
    remaining_hours: Optional[float] = None
    reset_at: Optional[str] = None
    reset_timezone: Optional[str] = None

class WalletInfo(BaseModel):
    balance_freebucks: Optional[float] = None
    balance_hours: Optional[float] = None
    monthly_bonus: Optional[float] = None

class BalanceInfo(BaseModel):
    total_freebucks: Optional[float] = None
    total_hours: Optional[float] = None
    daily: Optional[DailyAllowanceInfo] = None
    wallet: Optional[WalletInfo] = None
    last_refund: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    source: Optional[str] = None
    error: Optional[Any] = None

class AccountInfo(BaseModel):
    access_tier: Optional[str] = None
    country_code: Optional[str] = None
    country_verified: Optional[bool] = None
    status: Optional[str] = None
    source: Optional[str] = None
    error: Optional[Any] = None

class PricingResponse(BaseModel):
    account: Optional[AccountInfo] = None
    balance: Optional[BalanceInfo] = None
    subscription_tiers: List[Dict[str, Any]] = Field(default_factory=list)
    models_count: int
    models: List[ModelObject]
    status: Optional[str] = None
    source: Optional[str] = None
    error: Optional[Any] = None
    account_status: Optional[str] = None
    balance_status: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
