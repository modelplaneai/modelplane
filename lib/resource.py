"""Resource serialization helpers.

TODO: Contribute model_to_dict and update_status upstream to the Crossplane
Python Function SDK. The SDK's resource.update() handles model_to_dict
internally but doesn't expose it as a standalone function. update_status
has no SDK equivalent.
"""

import pydantic


def model_to_dict(model: pydantic.BaseModel) -> dict:
    """Serialize a Pydantic model to a dict, preserving apiVersion and kind.

    Pydantic's model_dump(exclude_defaults=True) drops apiVersion and kind
    when they equal the model's defaults. This matches the behavior of the
    SDK's resource.update(), which re-adds them after dumping.

    Use this for test assertions where you want to match only the fields you
    explicitly set. For test fixtures (extraResources, observedResources),
    use model_to_fixture instead.
    """
    data = model.model_dump(exclude_defaults=True, warnings=False)
    if hasattr(model, "apiVersion"):
        data["apiVersion"] = model.apiVersion
    if hasattr(model, "kind"):
        data["kind"] = model.kind
    return data


def model_to_fixture(model: pydantic.BaseModel) -> dict:
    """Serialize a Pydantic model to a dict for use as a test fixture.

    Unlike model_to_dict, this uses exclude_none=True so that fields with
    default values (e.g. spec.backend="KServe") are preserved. This is
    correct for extraResources and observedResources where the fixture
    represents a fully-realized resource with defaults applied.
    """
    data = model.model_dump(exclude_none=True, warnings=False)
    if hasattr(model, "apiVersion"):
        data["apiVersion"] = model.apiVersion
    if hasattr(model, "kind"):
        data["kind"] = model.kind
    return data


def update_status(r, status: pydantic.BaseModel) -> None:
    """Update a resource's status using a typed Pydantic Status model.

    Centralizes the serialization mode for status writes. Uses
    exclude_none=True so every explicitly set field is emitted, even if it
    equals the model default, but fields left as None are omitted.
    """
    # TODO: Remove this lazy import once the up CLI's Python test runner
    # (uptest-pyrunner) includes crossplane-function-sdk-python. The test
    # runner only installs pydantic, so importing the SDK at module level
    # crashes test manifest generation. Functions and tests share this lib
    # via symlinks, so any top-level SDK import breaks tests.
    from crossplane.function import resource  # noqa: PLC0415

    resource.update(r, {"status": status.model_dump(exclude_none=True)})
