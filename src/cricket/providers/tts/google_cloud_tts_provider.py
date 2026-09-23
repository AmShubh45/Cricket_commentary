"""Google Cloud TTS provider — high-quality WaveNet/Neural2 Hindi voices.

Uses Google Cloud Text-to-Speech API which offers:
  - WaveNet voices:  ~4M chars/month free
  - Neural2 voices:  ~1M chars/month free

Voices for Hindi:
  - hi-IN-Wavenet-A (female)
  - hi-IN-Wavenet-B (male)
  - hi-IN-Wavenet-C (male)
  - hi-IN-Wavenet-D (female)
  - hi-IN-Neural2-A (female)
  - hi-IN-Neural2-B (male)
  - hi-IN-Neural2-C (male)
  - hi-IN-Neural2-D (female)

Setup:
  1. Enable Cloud Text-to-Speech API in Google Cloud Console
  2. Create a service account and download JSON key
  3. Set env: GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
     OR set GOOGLE_TTS_API_KEY for API key auth
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from mutagen.mp3 import MP3

from config.settings import TTSSettings
from cricket.domain.enums import EmotionTag, EventTier, EventType
from cricket.domain.models import AudioChunk, CommentaryLine

logger = logging.getLogger(__name__)

# Pitch and speaking rate per emotion for natural broadcast delivery
_EMOTION_PARAMS: dict[EmotionTag, dict[str, float]] = {
    EmotionTag.NEUTRAL:       {"speaking_rate": 1.05, "pitch": 0.0},
    EmotionTag.EXCITED:       {"speaking_rate": 1.15, "pitch": 3.0},
    EmotionTag.DRAMATIC:      {"speaking_rate": 1.08, "pitch": 2.0},
    EmotionTag.DISAPPOINTED:  {"speaking_rate": 0.92, "pitch": -2.0},
    EmotionTag.CELEBRATORY:   {"speaking_rate": 1.18, "pitch": 4.0},
    EmotionTag.TENSE:         {"speaking_rate": 1.10, "pitch": 1.5},
    EmotionTag.CALM:          {"speaking_rate": 0.95, "pitch": -1.0},
    EmotionTag.ANGRY:         {"speaking_rate": 1.12, "pitch": 2.5},
}

# Default WaveNet voice (male, good for commentary)
_DEFAULT_VOICE = "hi-IN-Wavenet-B"


class GoogleCloudTTSProvider:
    """Google Cloud TTS provider using REST API with API key or service account."""

    def __init__(self, settings: TTSSettings | None = None, voice: str | None = None) -> None:
        self._api_key = os.getenv("GOOGLE_TTS_API_KEY", "")
        self._voice = voice or os.getenv("GOOGLE_TTS_VOICE", _DEFAULT_VOICE)
        self._output_format = settings.output_format if settings else "mp3"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))

        # Use google-cloud library if available, otherwise REST API
        self._use_rest = True
        try:
            if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
                from google.cloud import texttospeech  # noqa: F401
                self._use_rest = False
                logger.info("Google Cloud TTS: using service account credentials")
        except ImportError:
            pass

        if self._use_rest and not self._api_key:
            logger.warning(
                "Google Cloud TTS: no API key or credentials found. "
                "Set GOOGLE_TTS_API_KEY or GOOGLE_APPLICATION_CREDENTIALS."
            )

    async def synthesize(self, commentary: CommentaryLine | str, output_path: str) -> AudioChunk:
        """Synthesize Hindi text to speech using Google Cloud TTS."""
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if isinstance(commentary, str):
            commentary = CommentaryLine(
                text=commentary.strip(),
                emotion=EmotionTag.NEUTRAL,
                event_type=EventType.DOT_BALL,
                tier=EventTier.ROUTINE,
                source="preview",
                match_id="",
                over_display="",
            )

        params = _EMOTION_PARAMS.get(commentary.emotion, _EMOTION_PARAMS[EmotionTag.NEUTRAL])
        cleaned_text = commentary.text.strip()

        try:
            if self._use_rest:
                await self._synthesize_rest(cleaned_text, output_path, params)
            else:
                await self._synthesize_grpc(cleaned_text, output_path, params)

            duration_ms = self._get_audio_duration_ms(output_path)

            logger.info(
                "Google Cloud TTS synthesized: %d chars → %dms audio (voice=%s, %s)",
                len(cleaned_text), duration_ms, self._voice, commentary.emotion,
            )

            return AudioChunk(
                commentary=commentary,
                audio_path=output_path,
                duration_ms=duration_ms,
                tts_provider="google_cloud_tts",
                tts_cost_usd=0.0,  # Free tier
            )
        except Exception:
            logger.exception("Google Cloud TTS synthesis failed for: %s", cleaned_text[:50])
            raise

    async def _synthesize_rest(self, text: str, output_path: str, params: dict) -> None:
        """Synthesize using REST API with API key."""
        url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={self._api_key}"

        payload = {
            "input": {"text": text},
            "voice": {
                "languageCode": "hi-IN",
                "name": self._voice,
            },
            "audioConfig": {
                "audioEncoding": "MP3",
                "speakingRate": params["speaking_rate"],
                "pitch": params["pitch"],
                "sampleRateHertz": 24000,
            },
        }

        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()

        import base64
        audio_content = base64.b64decode(data["audioContent"])
        Path(output_path).write_bytes(audio_content)

    async def _synthesize_grpc(self, text: str, output_path: str, params: dict) -> None:
        """Synthesize using google-cloud-texttospeech library (gRPC)."""
        from google.cloud import texttospeech

        client = texttospeech.TextToSpeechClient()

        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(
            language_code="hi-IN",
            name=self._voice,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=params["speaking_rate"],
            pitch=params["pitch"],
            sample_rate_hertz=24000,
        )

        response = client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )

        Path(output_path).write_bytes(response.audio_content)

    async def healthcheck(self) -> bool:
        """Check if Google Cloud TTS is configured and reachable."""
        if self._use_rest and not self._api_key:
            return False
        try:
            if self._use_rest:
                url = f"https://texttospeech.googleapis.com/v1/voices?key={self._api_key}&languageCode=hi-IN"
                resp = await self._client.get(url)
                return resp.status_code == 200
            else:
                from google.cloud import texttospeech
                client = texttospeech.TextToSpeechClient()
                client.list_voices(language_code="hi-IN")
                return True
        except Exception:
            return False

    def estimate_cost(self, text: str) -> float:
        """Google Cloud TTS WaveNet free tier = $0.00 within limits."""
        return 0.0

    @staticmethod
    def _get_audio_duration_ms(audio_path: str) -> int:
        """Get audio file duration in milliseconds."""
        try:
            audio = MP3(audio_path)
            return int(audio.info.length * 1000)
        except Exception:
            return 0

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
