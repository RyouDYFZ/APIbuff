import time
from typing import List, Optional, Dict, Any, Union
from pydantic import BaseModel, Field
from .catalog import catalog_service

def resolve_model_id(requested_model: str) -> str:
    """Resolves an incoming model name, slug, or alias to primary slug."""
    resolved = catalog_service.resolve_model(requested_model)
    if resolved:
        return resolved["primarySlug"]
    return "mimo-v2.6-flash-fast"

# Pydantic models for OpenAI API compatibility
class ChatMessage(BaseModel):
    role: str
    content: str
    name: Optional[str] = None

class ChatCompletionRequest(BaseModel):
    model_config = {"extra": "allow"}
    model: str = "mimo-v2.6-flash-fast"
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    workspace: Optional[str] = None
    cwd: Optional[str] = None
    project_dir: Optional[str] = None
    session_id: Optional[str] = None
    reset_context: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None

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
    usage: UsageInfo = Field(default_factory=UsageInfo)

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
    freebucks_per_hour: Optional[int] = None
    hours_equivalent: Optional[float] = None
    is_free: bool = False
    notice: Optional[str] = None
    off_peak: Optional[Dict[str, Any]] = None

class ModelCapabilities(BaseModel):
    context_window: int = 128000
    multimodal: bool = False
    efforts: List[str] = Field(default_factory=list)
    default_effort: Optional[str] = None
    access: str = "open"
    plan_required: bool = False
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

class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelObject]

# Pricing and balance schemas
class DailyAllowanceInfo(BaseModel):
    limit_freebucks: int
    limit_hours: float
    spent_freebucks: int
    spent_hours: float
    remaining_freebucks: int
    remaining_hours: float
    reset_at: Optional[str] = None
    reset_timezone: Optional[str] = None

class WalletInfo(BaseModel):
    balance_freebucks: int
    balance_hours: float
    monthly_bonus: int = 0

class BalanceInfo(BaseModel):
    total_freebucks: int
    total_hours: float
    daily: DailyAllowanceInfo
    wallet: WalletInfo
    last_refund: Optional[Dict[str, Any]] = None

class AccountInfo(BaseModel):
    access_tier: str
    country_code: str
    country_verified: bool

class PricingResponse(BaseModel):
    account: AccountInfo
    balance: BalanceInfo
    subscription_tiers: List[Dict[str, Any]] = Field(default_factory=list)
    models_count: int
    models: List[ModelObject]
