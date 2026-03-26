"""Resource serialization helpers.

TODO: Contribute model_to_dict upstream to the Crossplane Python Function SDK.
The SDK's resource.update() does this internally but doesn't expose a
standalone function for it.
"""

import pydantic


def model_to_dict(model: pydantic.BaseModel) -> dict:
    """Serialize a Pydantic model to a dict, preserving apiVersion and kind.

    Pydantic's model_dump(exclude_defaults=True) drops apiVersion and kind
    when they equal the model's defaults. This matches the behavior of the
    SDK's resource.update(), which re-adds them after dumping.
    """
    data = model.model_dump(exclude_defaults=True, warnings=False)
    if hasattr(model, "apiVersion"):
        data["apiVersion"] = model.apiVersion
    if hasattr(model, "kind"):
        data["kind"] = model.kind
    return data
