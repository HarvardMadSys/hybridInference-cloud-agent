"""A new task starts on the model its owner last chose, or the one they pinned."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cloud_agent.model_preference import (
    ModelPreferenceView,
    UpdateModelPreference,
    deployment_default_model,
    resolve_model,
)


def test_the_pin_outranks_the_last_used_model():
    view = ModelPreferenceView.of("pinned-model", "used-model")

    assert view.default_model == "pinned-model"
    assert view.last_model == "used-model"
    # What the composer pre-selects: the explicit choice, not history.
    assert view.effective_model == "pinned-model"


def test_no_pin_follows_the_last_used_model():
    assert ModelPreferenceView.of(None, "used-model").effective_model == "used-model"


def test_an_owner_who_never_chose_has_no_effective_model():
    view = ModelPreferenceView.of(None, None)

    assert view.effective_model is None
    # Null means "no choice yet", which the composer reads as "keep your own
    # starting point" — never as "start on the empty string".
    assert view.model_dump() == {
        "default_model": None,
        "last_model": None,
        "last_model_connection_id": None,
        "effective_model": None,
        "deployment_default": None,
    }


@pytest.mark.parametrize(
    "pinned,last,deployment,models,expected",
    [
        # The pin leads whenever the catalog still offers it.
        ("a", "b", "c", ["a", "b", "c"], "a"),
        # An offered last-used model follows when there is no pin. It outranks
        # the deployment default: an administrator changing one setting must
        # not silently move existing users onto a different model.
        (None, "b", "c", ["a", "b", "c"], "b"),
        # The deployment default is what a user with no history starts on.
        (None, None, "c", ["a", "b", "c"], "c"),
        # A retired pin does not shadow a lower layer that is still offered.
        ("retired", "b", "c", ["a", "b", "c"], "b"),
        ("retired", "also-retired", "c", ["a", "b", "c"], "c"),
        # Every layer gone, or never set: no pre-selection at all.
        ("retired", "also-retired", "gone-too", ["a", "b", "c"], None),
        (None, None, None, ["a", "b", "c"], None),
        # An unreadable catalog withholds every choice — the deployment default
        # included: the picker must not pre-select something the create
        # endpoint may refuse.
        ("a", "b", "c", [], None),
        # The catalog is readable but nothing was ever chosen. Deliberately
        # null: the first model in registry order is NOT a fallback, because
        # the model decides cost, speed and capability and choosing one for the
        # owner runs their task on something they never picked.
        (None, None, None, ["first", "second"], None),
        # Same when every stored choice has been retired: the catalog order is
        # not an answer either.
        ("gone", "also-gone", "gone-too", ["first", "second"], None),
    ],
)
def test_the_first_offered_layer_wins(pinned, last, deployment, models, expected):
    assert resolve_model(models, ModelPreferenceView.of(pinned, last), deployment) == expected


def test_pinning_requires_the_field_but_accepts_null_as_clearing():
    assert UpdateModelPreference(default_model=None).default_model is None
    assert UpdateModelPreference(default_model="model-a").default_model == "model-a"

    # Omitting the field is not the same act as sending null, and must not
    # quietly clear a saved pin.
    with pytest.raises(ValidationError):
        UpdateModelPreference.model_validate({})
    with pytest.raises(ValidationError):
        UpdateModelPreference.model_validate({"default_model": "x" * 201})
    with pytest.raises(ValidationError):
        UpdateModelPreference.model_validate({"default_model": "a", "user_id": "someone-else"})


def test_the_deployment_default_is_an_admin_setting(monkeypatch):
    """It is persisted configuration, not a constant in the frontend.

    The composer used to hardcode one model name, so an operator had no way to
    choose the deployment's starting point at all.
    """
    from cloud_agent import settings

    setting = settings.SETTINGS_BY_KEY["default_model"]
    assert setting.kind == "string"
    assert setting.category == "Execution"
    assert setting.env == "AGENT_DEFAULT_MODEL"
    assert setting.validate("glm-5.1") == "glm-5.1"
    # Empty is a real value: it means this deployment offers no default at all,
    # and an owner who has chosen nothing is then asked to pick.
    assert setting.validate("") == ""

    # The shipped default preserves what the composer used to hardcode, so
    # upgrading changes no behaviour — it only moves the choice into settings.
    # A deployment whose gateway does not offer this model skips it like any
    # other unavailable layer, so a wrong default cannot strand anyone.
    assert setting.default == "deepseek-v4-flash"

    # A saved value is read from the settings snapshot, not the environment, so
    # a change applies without a restart.
    monkeypatch.setattr(settings, "_values", {"default_model": "  qwen-next  "})
    assert deployment_default_model() == "qwen-next"
    monkeypatch.setattr(settings, "_values", {"default_model": ""})
    assert deployment_default_model() is None


@pytest.mark.parametrize(
    ("environment", "imported"),
    [(None, "deepseek-v4-flash"), ("", ""), (" glm-5.1 ", "glm-5.1")],
)
def test_an_empty_environment_value_imports_no_default(monkeypatch, environment, imported):
    """``AGENT_DEFAULT_MODEL=""`` turns the default off, as documented.

    Unset keeps the shipped default. Compose passes the variable with
    ``${AGENT_DEFAULT_MODEL-…}``, so an operator's empty value reaches here.
    """
    from cloud_agent import settings

    monkeypatch.delenv("AGENT_DEFAULT_MODEL", raising=False)
    if environment is not None:
        monkeypatch.setenv("AGENT_DEFAULT_MODEL", environment)
    monkeypatch.setattr(settings, "_values", None)
    stored = settings.initial_settings(keys={"default_model"})
    assert stored["default_model"] == imported
    # Before the first snapshot, the environment is read the same way.
    assert deployment_default_model() == (imported or None)
