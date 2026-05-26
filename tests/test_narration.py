import pytest

from app.services.narration import (
    NarrationInputError,
    NarrationProviderError,
    NarrationUnavailableError,
    OpenAINarrationProvider,
    validate_narration_input,
)


def test_validate_narration_input_trims_text_and_accepts_within_cap():
    assert (
        validate_narration_input(text="  Study this factoring example.  ", max_chars=64)
        == "Study this factoring example."
    )


def test_validate_narration_input_rejects_empty_text():
    with pytest.raises(NarrationInputError, match="Enter text to narrate"):
        validate_narration_input(text="   ", max_chars=64)


def test_validate_narration_input_rejects_text_over_cap():
    with pytest.raises(NarrationInputError, match="10 characters or fewer"):
        validate_narration_input(text="too long for this cap", max_chars=10)


def test_openai_narration_provider_requires_configuration():
    provider = OpenAINarrationProvider(
        api_key="",
        api_base_url="https://api.openai.com/v1",
        model_name="gpt-4o-mini-tts",
        voice="coral",
        timeout_seconds=60,
    )

    assert not provider.is_configured()
    with pytest.raises(NarrationUnavailableError, match="not configured"):
        provider.generate_audio(text="Hello")


def test_openai_narration_provider_rejects_empty_audio(monkeypatch: pytest.MonkeyPatch):
    class _EmptyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return b""

    def _fake_urlopen(request, timeout):
        return _EmptyResponse()

    monkeypatch.setattr("app.services.narration.urllib_request.urlopen", _fake_urlopen)
    provider = OpenAINarrationProvider(
        api_key="sk-test",
        api_base_url="https://api.openai.com/v1",
        model_name="gpt-4o-mini-tts",
        voice="coral",
        timeout_seconds=60,
    )

    with pytest.raises(NarrationProviderError, match="empty audio"):
        provider.generate_audio(text="Hello")
