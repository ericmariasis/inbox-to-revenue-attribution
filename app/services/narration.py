import json
from dataclasses import dataclass
from typing import Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request

from app.core.config import Settings, get_settings

NARRATION_AUDIO_MEDIA_TYPE = "audio/mpeg"


class NarrationUnavailableError(RuntimeError):
    pass


class NarrationInputError(ValueError):
    pass


class NarrationProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class NarrationAudioResult:
    audio: bytes
    media_type: str = NARRATION_AUDIO_MEDIA_TYPE


class NarrationProvider(Protocol):
    def is_configured(self) -> bool: ...

    def generate_audio(self, *, text: str) -> NarrationAudioResult: ...


class OpenAINarrationProvider:
    def __init__(
        self,
        *,
        api_key: str,
        api_base_url: str,
        model_name: str,
        voice: str,
        timeout_seconds: int,
    ) -> None:
        self._api_key = api_key.strip()
        self._api_base_url = api_base_url.rstrip("/")
        self._model_name = model_name.strip()
        self._voice = voice.strip()
        self._timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self._api_key and self._api_base_url and self._model_name and self._voice)

    def generate_audio(self, *, text: str) -> NarrationAudioResult:
        if not self.is_configured():
            raise NarrationUnavailableError("Narration provider is not configured")

        body = {
            "model": self._model_name,
            "voice": self._voice,
            "input": text,
            "response_format": "mp3",
        }
        request = urllib_request.Request(
            url=f"{self._api_base_url}/audio/speech",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=self._timeout_seconds) as response:
                audio = response.read()
        except urllib_error.HTTPError as exc:
            raise NarrationProviderError(
                f"OpenAI narration failed with status {exc.code}"
            ) from exc
        except urllib_error.URLError as exc:
            raise NarrationProviderError(
                f"OpenAI narration could not reach the API: {exc.reason}"
            ) from exc

        if not audio:
            raise NarrationProviderError("OpenAI narration returned empty audio")
        return NarrationAudioResult(audio=audio)


def build_default_narration_provider(
    *,
    settings: Settings | None = None,
) -> NarrationProvider:
    resolved_settings = settings or get_settings()
    return OpenAINarrationProvider(
        api_key=resolved_settings.openai_api_key,
        api_base_url=resolved_settings.openai_api_base_url,
        model_name=resolved_settings.openai_tts_model,
        voice=resolved_settings.openai_tts_voice,
        timeout_seconds=resolved_settings.openai_tts_timeout_seconds,
    )


def validate_narration_input(*, text: str, max_chars: int) -> str:
    normalized_text = text.strip()
    if not normalized_text:
        raise NarrationInputError("Enter text to narrate.")
    if len(normalized_text) > max_chars:
        raise NarrationInputError(
            f"Text must be {max_chars:,} characters or fewer for this first version."
        )
    return normalized_text
