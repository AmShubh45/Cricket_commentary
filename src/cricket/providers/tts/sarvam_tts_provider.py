"""Sarvam AI TTS provider -- natural Indian-language speech synthesis.

Sarvam AI's Bulbul model is purpose-built for Indian languages and offers:
  - Natural Hindi prosody with proper Devanagari rendering
  - Hinglish code-switching (cricket terms like 'six', 'boundary', 'LBW' pronounced correctly)
  - Broadcast-ready Indian commentary voices
  - Fast response times
  - Free tier available at https://www.sarvam.ai/

Set SARVAM_API_KEY in your .env file to activate.
If no key is set, this provider raises RuntimeError and the factory falls back to Edge TTS.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import httpx
from mutagen.mp3 import MP3

from config.settings import TTSSettings
from cricket.domain.enums import EmotionTag, EventTier, EventType
from cricket.domain.models import AudioChunk, CommentaryLine
from cricket.providers.tts.edge_tts_provider import EdgeTTSProvider

logger = logging.getLogger(__name__)

_SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"

# Active high-quality Sarvam Bulbul model
_MODEL = "bulbul:v3"

# Valid speakers supported by bulbul:v3:
# Male:   ratan, ashutosh, aditya, rohan, rahul, amit, dev, kabir, shubh, advait, anand, tarun, sunny
# Female: ritu, priya, neha, pooja, simran, kavya, ishita, shreya, roopa, tanya, shruti, suhani
_VALID_SPEAKERS = {
    "ratan", "ashutosh", "aditya", "rohan", "rahul", "amit", "dev",
    "kabir", "shubh", "advait", "anand", "tarun", "sunny", "mani",
    "ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita",
    "shreya", "roopa", "tanya", "shruti", "suhani", "rehan", "soham", "rupali"
}

# 'ratan' is deep, mature, and authoritative — perfect for Hindi cricket commentary
_DEFAULT_SPEAKER = "ratan"

# Emotion-mapped voices for dynamic commentary:
# ratan: authoritative, energetic main commentary
# anand: analytical, composed for breaks and session reviews
_EMOTION_VOICES: dict[EmotionTag, str] = {
    EmotionTag.NEUTRAL:      "ratan",
    EmotionTag.EXCITED:      "ratan",
    EmotionTag.DRAMATIC:     "ratan",
    EmotionTag.DISAPPOINTED: "ratan",
    EmotionTag.CELEBRATORY:  "ratan",
    EmotionTag.TENSE:        "ratan",
    EmotionTag.CALM:         "anand",
    EmotionTag.ANGRY:        "ratan",
}


class SarvamTTSProvider:
    """Sarvam AI TTS provider — using bulbul:v3 with natural Hindi voices and automatic Edge-TTS fallback."""

    def __init__(self, settings: TTSSettings) -> None:
        self._fallback_provider = EdgeTTSProvider(settings, voice="hi-IN-MadhurNeural")
        self._fallback_active = False

        self._api_key = os.getenv("SARVAM_API_KEY", "").strip()
        if not self._api_key:
            logger.warning(
                "SARVAM_API_KEY not set. Defaulting directly to Edge-TTS fallback."
            )
            self._fallback_active = True

        # Check if user specified a valid voice in settings/env
        user_voice = (settings.voice_id or "").strip().lower()
        if user_voice in _VALID_SPEAKERS:
            self._speaker_override = user_voice
        else:
            # Deprecated names (amartya, arvind, etc.) or edge voices fall back to default
            self._speaker_override = ""

        active_speaker = self._speaker_override or f"emotion-mapped ({_DEFAULT_SPEAKER} / anand)"
        logger.info(
            "Sarvam TTS v3 initialized: model=%s, speaker=%s (Edge-TTS fallback ready)",
            _MODEL, active_speaker,
        )
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0))

    async def synthesize(self, commentary: "CommentaryLine | str", output_path: str) -> "AudioChunk":
        """Convert Hindi commentary to speech using Sarvam bulbul:v3."""
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if isinstance(commentary, str):
            commentary = CommentaryLine(
                text=commentary.strip(),
                emotion=EmotionTag.EXCITED,
                event_type=EventType.DOT_BALL,
                tier=EventTier.ROUTINE,
                source="preview",
                match_id="",
                over_display="",
            )

        if self._fallback_active:
            return await self._fallback_provider.synthesize(commentary, output_path)

        speaker = self._speaker_override or _EMOTION_VOICES.get(commentary.emotion, _DEFAULT_SPEAKER)
        text = commentary.text.strip()

        # Natural prosody: slight variations by emotion
        pace = 1.0
        loudness = 1.3
        if commentary.emotion in (EmotionTag.EXCITED, EmotionTag.CELEBRATORY):
            pace = 1.05
            loudness = 1.5
        elif commentary.emotion in (EmotionTag.DRAMATIC, EmotionTag.TENSE):
            pace = 0.95
            loudness = 1.4
        elif commentary.emotion == EmotionTag.CALM:
            pace = 0.95
            loudness = 1.2

        payload = {
            "inputs": [text],
            "target_language_code": "hi-IN",
            "speaker": speaker,
            "pitch": 0.0,
            "pace": pace,
            "loudness": loudness,
            "speech_sample_rate": 22050,
            "enable_preprocessing": True,
            "model": _MODEL,
        }
        headers = {
            "api-subscription-key": self._api_key,
            "Content-Type": "application/json",
        }

        try:
            resp = await self._client.post(_SARVAM_TTS_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            audio_b64 = data.get("audios", [None])[0]
            if not audio_b64:
                raise ValueError("Sarvam API returned no audio data")

            audio_bytes = base64.b64decode(audio_b64)
            wav_path = output_path.replace(".mp3", ".wav")
            Path(wav_path).write_bytes(audio_bytes)

            # Convert WAV to MP3 using ffmpeg
            import subprocess
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", wav_path, "-codec:a", "libmp3lame",
                 "-qscale:a", "2", "-ar", "22050", output_path],
                capture_output=True, timeout=15,
            )
            if result.returncode != 0:
                import shutil
                shutil.move(wav_path, output_path)
                logger.warning("ffmpeg WAV to MP3 conversion failed, serving WAV as MP3")
            else:
                try:
                    Path(wav_path).unlink(missing_ok=True)
                except Exception:
                    pass

            duration_ms = self._get_audio_duration_ms(output_path)
            logger.info(
                "Sarvam TTS synthesized: %d chars -> %dms (speaker=%s pace=%.2f loudness=%.1f)",
                len(text), duration_ms, speaker, pace, loudness,
            )
            return AudioChunk(
                commentary=commentary,
                audio_path=output_path,
                duration_ms=duration_ms,
                tts_provider="sarvam_bulbul_v3",
                tts_cost_usd=0.0,
            )

        except httpx.HTTPStatusError as e:
            err_text = ""
            try:
                err_text = e.response.text[:200]
            except Exception:
                pass

            if e.response.status_code in (402, 403) or "quota" in err_text.lower() or "credit" in err_text.lower():
                logger.warning(
                    "⚠️ Sarvam AI credits exhausted / payment required (HTTP %d: %s). "
                    "Permanently switching to Edge TTS fallback (hi-IN-MadhurNeural) for uninterrupted live stream!",
                    e.response.status_code, err_text,
                )
                self._fallback_active = True
            else:
                logger.warning(
                    "⚠️ Sarvam API HTTP %d error (%s). Falling back to Edge-TTS for this audio chunk...",
                    e.response.status_code, err_text,
                )
            return await self._fallback_provider.synthesize(commentary, output_path)

        except Exception as exc:
            logger.warning("⚠️ Sarvam TTS synthesis failed (%s). Falling back to Edge-TTS...", exc)
            return await self._fallback_provider.synthesize(commentary, output_path)

    async def healthcheck(self) -> bool:
        """Verify Sarvam API connectivity, falling back to Edge TTS if unavailable."""
        if self._fallback_active or not self._api_key:
            return await self._fallback_provider.healthcheck()

        try:
            resp = await self._client.post(
                _SARVAM_TTS_URL,
                json={
                    "inputs": ["परीक्षण"],
                    "target_language_code": "hi-IN",
                    "speaker": _DEFAULT_SPEAKER,
                    "model": _MODEL,
                },
                headers={
                    "api-subscription-key": self._api_key,
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 200:
                return True
            logger.warning("Sarvam TTS healthcheck returned %d, falling back to Edge TTS", resp.status_code)
            return await self._fallback_provider.healthcheck()
        except Exception:
            logger.exception("Sarvam TTS healthcheck failed, falling back to Edge TTS")
            return await self._fallback_provider.healthcheck()

    def estimate_cost(self, text: str) -> float:
        return 0.0

    @staticmethod
    def _get_audio_duration_ms(audio_path: str) -> int:
        try:
            audio = MP3(audio_path)
            return int(audio.info.length * 1000)
        except Exception:
            logger.warning("Could not determine audio duration for %s", audio_path)
            return 4000
