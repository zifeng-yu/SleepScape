"""Construct only explicitly supported model providers."""

from sleepscape.models.base import ModelClient, ModelError
from sleepscape.models.openai_compatible import OpenAICompatibleClient
from sleepscape.models.settings import ModelSettings


def create_client(settings: ModelSettings) -> ModelClient:
    if settings.provider != "openai_compatible":
        raise ModelError("model_configuration_invalid")
    return OpenAICompatibleClient(settings)
