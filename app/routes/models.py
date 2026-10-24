from fastapi import APIRouter, HTTPException
from ..models import ModelList, ModelObject, PricingResponse
from ..catalog import catalog_service

router = APIRouter()

@router.get("/v1/models", response_model=ModelList)
@router.get("/models", response_model=ModelList)
async def list_models():
    """Lists all dynamically retrieved models compatible with OpenAI specification, enriched with pricing and capabilities."""
    models_data = catalog_service.get_all_models()
    return ModelList(object="list", data=models_data)

@router.get("/v1/models/{model_id}", response_model=ModelObject)
@router.get("/models/{model_id}", response_model=ModelObject)
async def get_model(model_id: str):
    """Retrieves metadata, pricing, and capabilities for a specific model."""
    resolved = catalog_service.resolve_model(model_id)
    if not resolved:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found in catalog")
    
    # Return formatted ModelObject
    models_data = catalog_service.get_all_models()
    for m in models_data:
        if m["key"] == resolved["key"]:
            return m
    raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

@router.get("/v1/pricing", response_model=PricingResponse)
@router.get("/pricing", response_model=PricingResponse)
async def get_pricing():
    """Returns detailed account balances, daily Freebucks/hour quotas, rate cards, and subscription plans."""
    return catalog_service.get_pricing_overview()
