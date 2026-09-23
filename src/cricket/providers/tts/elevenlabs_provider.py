"""ElevenLabs TTS provider — premium upgrade path.

Requires: pip install cricket-commentary[elevenlabs]
Provides higher quality Hindi voice synthesis with native emotion tag
support via ElevenLabs' SSML-like formatting.

Cost: ~$0.10/1K chars (Multilingual v3) or ~$0.05/1K chars (Flash)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from config.settings import TTSSettings
from cricket.domain.enums import EmotionTag
from cricket.domain.models import AudioChunk, CommentaryLine

logger = logging.getLogger(__name__)

# Cost per 1,000 characters (USD)
_MODEL_COSTS: dict[str, float] = {
    "eleven_multilingual_v2": 0.10,
    "eleven_flash_v2_5": 0.05,
    "eleven_turbo_v2_5": 0.05,
}

# ElevenLabs voice settings per emotion
_EMOTION_SETTINGS: dict[EmotionTag, dict[str, Any]] = {
    EmotionTag.NEUTRAL: {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0},
    EmotionTag.EXCITED: {"stability": 0.3, "similarity_boost": 0.85, "style": 0.7},
    EmotionTag.DRAMATIC: {"stability": 0.4, "similarity_boost": 0.80, "style": 0.6},
    EmotionTag.DISAPPOINTED: {"stability": 0.6, "similarity_boost": 0.70, "style": 0.3},
    EmotionTag.CELEBRATORY: {"stability": 0.25, "similarity_boost": 0.90, "style": 0.8},
    EmotionTag.TENSE: {"stability": 0.45, "similarity_boost": 0.75, "style": 0.5},
    EmotionTag.CALM: {"stability": 0.7, "similarity_boost": 0.65, "style": 0.1},
    EmotionTag.ANGRY: {"stability": 0.3, "similarity_boost": 0.85, "style": 0.7},
}


class ElevenLabsTTSProvider:
    """ElevenLabs TTS provider implementing TTSProviderProtocol.

    Premium voice synthesis with emotion-aware voice modulation.
    Drop-in replacement for EdgeTTSProvider — same interface.

    Usage:
        provider = ElevenLabsTTSProvider(settings)
        audio = await provider.synthesize(commentary, "/output/audio/ball_1.mp3")
    """

    def __init__(self, settings: TTSSettings) -> None:
        try:
            from elevenlabs.client import AsyncElevenLabs
        except ImportError as e:
            raise ImportError(
                "ElevenLabs not installed. Install with: "
                "pip install cricket-commentary[elevenlabs]"
            ) from e

        self._client = AsyncElevenLabs(api_key=settings.elevenlabs_api_key)
        self._voice_id = settings.elevenlabs_voice_id
        self._model_id = settings.elevenlabs_model_id
        self._cost_per_1k = _MODEL_COSTS.get(self._model_id, 0.10)

        logger.info(
            "ElevenLabs TTS initialized: voice=%s, model=%s",
            self._voice_id,
            self._model_id,
        )

    async def synthesize(self, commentary: CommentaryLine, output_path: str) -> AudioChunk:
        """Convert Hindi commentary to speech with emotion-aware voice settings."""
        from elevenlabs import VoiceSettings

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Get emotion-specific voice settings
        emotion_settings = _EMOTION_SETTINGS.get(
            commentary.emotion,
            _EMOTION_SETTINGS[EmotionTag.NEUTRAL],
        )

        voice_settings = VoiceSettings(
            stability=emotion_settings["stability"],
            similarity_boost=emotion_settings["similarity_boost"],
            style=emotion_settings.get("style", 0.0),
            use_speaker_boost=True,
        )

        try:
            # Generate audio
            audio_generator = await self._client.text_to_speech.convert(
                text=commentary.text,
                voice_id=self._voice_id,
                model_id=self._model_id,
                voice_settings=voice_settings,
            )

            # Write audio to file
            audio_bytes = b""
            async for chunk in audio_generator:
                audio_bytes += chunk

            with open(output_path, "wb") as f:
                f.write(audio_bytes)

            # Calculate cost
            char_count = len(commentary.text)
            cost = self.estimate_cost(commentary.text)

            # Get duration using pydub
            duration_ms = self._get_duration(output_path)

            logger.info(
                "ElevenLabs TTS: %d chars → %dms audio, $%.4f (%s)",
                char_count,
                duration_ms,
                cost,
                commentary.emotion,
            )

            return AudioChunk(
                commentary=commentary,
                audio_path=output_path,
                duration_ms=duration_ms,
                tts_provider="elevenlabs",
                tts_cost_usd=cost,
            )

        except Exception:
            logger.exception("ElevenLabs synthesis failed: %s", commentary.text[:50])
            raise

    async def healthcheck(self) -> bool:
        """Verify ElevenLabs API connectivity."""
        try:
            models = await self._client.models.get_all()
            return len(models) > 0
        except Exception:
            logger.exception("ElevenLabs healthcheck failed")
            return False

    def estimate_cost(self, text: str) -> float:
        """Estimate USD cost for synthesizing the given text."""
        char_count = len(text)
        return round((char_count / 1000) * self._cost_per_1k, 6)

    @staticmethod
    def _get_duration(audio_path: str) -> int:
        """Get audio duration in milliseconds."""
        try:
            from mutagen.mp3 import MP3
            audio = MP3(audio_path)
            return int(audio.info.length * 1000)
        except Exception:
            logger.warning("Could not determine audio duration: %s", audio_path)
            return 0
