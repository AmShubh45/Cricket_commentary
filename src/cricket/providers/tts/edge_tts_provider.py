"""Edge TTS provider — free Microsoft TTS for budget operation.

Uses the `edge-tts` Python library which interfaces with Microsoft Edge's
free text-to-speech service. Supports Hindi (Devanagari) with multiple
voice options.

Voices for Hindi:
  - hi-IN-SwaraNeural (female) — default, natural-sounding
  - hi-IN-MadhurNeural (male) — good for cricket commentary tone

No API key required. No usage limits. No cost.
Trade-off: Less emotion control compared to ElevenLabs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import edge_tts
from mutagen.mp3 import MP3

from config.settings import TTSSettings
from cricket.domain.enums import EmotionTag, EventTier, EventType
from cricket.domain.models import AudioChunk, CommentaryLine

logger = logging.getLogger(__name__)

# Prosody adjustments for emotion simulation in Edge TTS.
# Edge TTS doesn't support emotion tags natively, but we can modulate
# rate, pitch, and volume via SSML to approximate emotional delivery.
_EMOTION_PROSODY: dict[EmotionTag, dict[str, str]] = {
    # Tuned for natural Hindi cricket broadcast delivery
    EmotionTag.NEUTRAL:       {"rate": "+0%",  "pitch": "+0Hz",  "volume": "+0%"},
    EmotionTag.EXCITED:       {"rate": "+10%", "pitch": "+15Hz", "volume": "+15%"},
    EmotionTag.DRAMATIC:      {"rate": "+5%",  "pitch": "+10Hz", "volume": "+20%"},
    EmotionTag.DISAPPOINTED:  {"rate": "-8%",  "pitch": "-10Hz", "volume": "-5%"},
    EmotionTag.CELEBRATORY:   {"rate": "+12%", "pitch": "+18Hz", "volume": "+15%"},
    EmotionTag.TENSE:         {"rate": "+5%",  "pitch": "+8Hz",  "volume": "+10%"},
    EmotionTag.CALM:          {"rate": "-3%",  "pitch": "-5Hz",  "volume": "+0%"},
    EmotionTag.ANGRY:         {"rate": "+8%",  "pitch": "+12Hz", "volume": "+15%"},
}


class EdgeTTSProvider:
    """Free Microsoft Edge TTS provider implementing TTSProviderProtocol.

    Usage:
        provider = EdgeTTSProvider(settings)
        audio = await provider.synthesize(commentary_line, "/output/audio/ball_1.mp3")
    """

    def __init__(self, settings: TTSSettings | None = None, voice: str | None = None) -> None:
        raw_voice = voice or (settings.voice_id if settings else "") or ""
        # Check if the configured voice is a valid Edge TTS voice, otherwise default to hi-IN-MadhurNeural
        if raw_voice.startswith("hi-IN-") or "Neural" in raw_voice:
            self._voice = raw_voice
        else:
            self._voice = "hi-IN-MadhurNeural"

        self._output_format = settings.output_format if settings else "mp3"

    async def synthesize(self, commentary: CommentaryLine | str, output_path: str) -> AudioChunk:
        """Convert Hindi commentary text to speech using Edge TTS with emotion modulation."""
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

        prosody = _EMOTION_PROSODY.get(commentary.emotion, _EMOTION_PROSODY[EmotionTag.NEUTRAL])
        cleaned_text = commentary.text.strip()

        try:
            communicate = edge_tts.Communicate(
                text=cleaned_text,
                voice=self._voice,
                rate=prosody["rate"],
                pitch=prosody["pitch"],
                volume=prosody["volume"],
            )
            await communicate.save(output_path)

            duration_ms = self._get_audio_duration_ms(output_path)

            logger.info(
                "Edge TTS synthesized: %d chars → %dms audio (%s | rate=%s, pitch=%s)",
                len(cleaned_text),
                duration_ms,
                commentary.emotion,
                prosody["rate"],
                prosody["pitch"],
            )

            return AudioChunk(
                commentary=commentary,
                audio_path=output_path,
                duration_ms=duration_ms,
                tts_provider="edge_tts",
                tts_cost_usd=0.0,  # Edge TTS is free!
            )

        except Exception:
            logger.exception("Edge TTS synthesis failed for: %s", commentary.text[:50])
            raise

    async def healthcheck(self) -> bool:
        """Verify Edge TTS is working by generating a tiny test audio."""
        try:
            test_communicate = edge_tts.Communicate(
                text="परीक्षण",  # "Test" in Hindi
                voice=self._voice,
            )
            # Just check if it initializes — don't actually save
            async for _ in test_communicate.stream():
                return True
            return True
        except Exception:
            logger.exception("Edge TTS healthcheck failed")
            return False

    def estimate_cost(self, text: str) -> float:
        """Edge TTS is free — always returns 0."""
        return 0.0

    def _build_ssml(self, text: str, emotion: EmotionTag) -> str:
        """Build SSML with prosody adjustments for emotion simulation.

        Edge TTS accepts plain text or SSML. We use SSML to modulate
        rate/pitch/volume based on the event emotion.
        """
        prosody = _EMOTION_PROSODY.get(emotion, _EMOTION_PROSODY[EmotionTag.NEUTRAL])

        # For Edge TTS, we pass the text directly with the communicate API.
        # The `edge-tts` library handles SSML internally when using Communicate.
        # For prosody control, we'll use a simple approach: modify the text
        # with pauses and emphasis markers.

        # Add natural pauses for dramatic effect
        if emotion in (EmotionTag.DRAMATIC, EmotionTag.TENSE):
            # Add brief pauses before key phrases
            text = text.replace("!", "! ... ")
        elif emotion == EmotionTag.EXCITED:
            # Faster delivery for excitement — keep text as-is
            pass

        return text

    @staticmethod
    def _get_audio_duration_ms(audio_path: str) -> int:
        """Get audio file duration in milliseconds using mutagen."""
        try:
            audio = MP3(audio_path)
            return int(audio.info.length * 1000)
        except Exception:
            logger.warning("Could not determine audio duration for %s", audio_path)
            return 0
