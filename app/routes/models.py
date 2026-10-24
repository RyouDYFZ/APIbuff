import asyncio
from fastapi import APIRouter
from ..models import ModelList, ModelObject, PricingResponse
from ..catalog import catalog_service
from ..exceptions import FreebuffException

router = APIRouter()


@router.get("/v1/models", response_model=ModelList)
@router.get("/models", response_model=ModelList)
async def list_models():
    """Return source-labelled catalog metadata, including unverified fallback rows."""
    return await asyncio.to_thread(catalog_service.get_catalog_overview)


@router.get("/v1/models/{model_id}", response_model=ModelObject)
@router.get("/models/{model_id}", response_model=ModelObject)
async def get_model(model_id: str):
    model = await asyncio.to_thread(catalog_service.get_model, model_id)
    if model is None:
        raise FreebuffException(
            f"Model '{model_id}' was not found in the catalog.",
            status_code=404, error_type="invalid_request_error", error_code="model_not_found",
        )
    return model


@router.get("/v1/pricing", response_model=PricingResponse)
@router.get("/pricing", response_model=PricingResponse)
async def get_pricing():
    """Default-profile pricing only; unavailable account values remain null."""
    return await asyncio.to_thread(catalog_service.get_pricing_overview)
