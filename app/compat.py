from typing import Any


def dump_model(model: Any, **kwargs: Any) -> dict[str, Any]:
    """Return a dict from either Pydantic v1 or v2 models."""
    if hasattr(model, "model_dump"):
        return model.model_dump(**kwargs)
    return model.dict(**kwargs)
