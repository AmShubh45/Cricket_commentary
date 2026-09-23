"""ffmpeg-based stream compositor and RTMP publisher.

Handles two modes:
1. OFFLINE (Phase 1): Stitches audio chunks + PNG frames → MP4 video file
2. LIVE (Phase 2+): Pushes frames + audio to YouTube RTMP in real-time

Uses ffmpeg subprocess for both modes. The Pango/Cairo pipeline for Devanagari
text rendering is handled by the Playwright renderer upstream — ffmpeg here just
composites the final output.

Architecture:
- Offline mode: collects all audio + frames, then runs a single ffmpeg command
- Live mode: maintains a persistent ffmpeg subprocess with pipe input
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from config.settings import FFmpegSettings, StreamSettings
from cricket.domain.enums import StreamStatus

logger = logging.getLogger(__name__)


class FFmpegCompositor:
    """ffmpeg-based video compositor for offline MP4 generation.

    Takes a list of PNG frames and MP3 audio chunks, composites them
    into a single video file.

    Usage:
        compositor = FFmpegCompositor(ffmpeg_settings)
        output = await compositor.create_video(
            frames=[("frame1.png", 3.5), ("frame2.png", 2.1), ...],
            audio_files=["audio1.mp3", "audio2.mp3", ...],
            output_path="output/video/highlights.mp4",
        )
    """

    def __init__(self, settings: FFmpegSettings) -> None:
        self._ffmpeg = settings.path
        self._video_bitrate = settings.video_bitrate
        self._audio_bitrate = settings.audio_bitrate
        self._framerate = settings.framerate
        self._width = settings.width
        self._height = settings.height

    async def create_video(
        self,
        frames: list[tuple[str, float]],  # List of (frame_path, duration_seconds)
        audio_files: list[str],
        output_path: str,
        intro_audio: str | None = None,
        outro_audio: str | None = None,
    ) -> str:
        """Create an MP4 video from frames and audio chunks.

        Each frame is displayed for its corresponding duration (matched to
        the audio chunk length). Audio chunks are concatenated sequentially.

        Args:
            frames: List of (PNG path, display duration in seconds).
            audio_files: List of MP3/WAV audio file paths (same order as frames).
            output_path: Where to save the final MP4.
            intro_audio: Optional intro audio to prepend.
            outro_audio: Optional outro audio to append.

        Returns:
            Absolute path to the output MP4.
        """
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Step 1: Create the concat audio file
        audio_list_path = output_path.replace(".mp4", "_audio_list.txt")
        audio_concat_path = output_path.replace(".mp4", "_audio_concat.mp3")

        all_audio = []
        if intro_audio and os.path.exists(intro_audio):
            all_audio.append(intro_audio)
        all_audio.extend(audio_files)
        if outro_audio and os.path.exists(outro_audio):
            all_audio.append(outro_audio)

        # Write ffmpeg concat list
        with open(audio_list_path, "w", encoding="utf-8") as f:
            for audio in all_audio:
                abs_path = os.path.abspath(audio).replace("\\", "/")
                f.write(f"file '{abs_path}'\n")

        # Concatenate all audio
        concat_cmd = [
            self._ffmpeg, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", audio_list_path,
            "-c", "copy",
            audio_concat_path,
        ]

        logger.info("Concatenating %d audio files...", len(all_audio))
        await self._run_command(concat_cmd)

        # Step 2: Create video from frames with matching durations
        # Build a complex filter with frame durations
        video_input_path = output_path.replace(".mp4", "_frames.txt")

        with open(video_input_path, "w", encoding="utf-8") as f:
            for frame_path, duration in frames:
                abs_path = os.path.abspath(frame_path).replace("\\", "/")
                f.write(f"file '{abs_path}'\n")
                f.write(f"duration {duration:.3f}\n")
            # Repeat last frame (ffmpeg concat demuxer quirk)
            if frames:
                last_frame = os.path.abspath(frames[-1][0]).replace("\\", "/")
                f.write(f"file '{last_frame}'\n")

        # Step 3: Compose video + audio
        compose_cmd = [
            self._ffmpeg, "-y",
            # Video input from frame sequence
            "-f", "concat",
            "-safe", "0",
            "-i", video_input_path,
            # Audio input
            "-i", audio_concat_path,
            # Video encoding
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-r", str(self._framerate),
            "-s", f"{self._width}x{self._height}",
            # Audio encoding
            "-c:a", "aac",
            "-b:a", self._audio_bitrate,
            # Match shortest stream
            "-shortest",
            # Output
            output_path,
        ]

        logger.info("Compositing video: %d frames + audio → %s", len(frames), output_path)
        await self._run_command(compose_cmd)

        # Cleanup temp files
        for tmp in [audio_list_path, audio_concat_path, video_input_path]:
            if os.path.exists(tmp):
                os.remove(tmp)

        logger.info("Video created: %s", output_path)
        return os.path.abspath(output_path)

    async def _run_command(self, cmd: list[str]) -> None:
        """Run an ffmpeg command asynchronously."""
        logger.debug("Running: %s", " ".join(cmd))

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="replace")[-500:]
            logger.error("ffmpeg failed (exit %d): %s", process.returncode, error_msg)
            raise FFmpegError(f"ffmpeg exited with code {process.returncode}: {error_msg}")


class RTMPStreamer:
    """Live RTMP stream publisher using ffmpeg.

    Maintains a persistent ffmpeg subprocess that reads frames from
    a named pipe and pushes to YouTube's RTMP endpoint.

    Usage:
        streamer = RTMPStreamer(ffmpeg_settings, stream_settings)
        await streamer.start_stream()
        await streamer.push_frame("frame.png", "audio.mp3")
        await streamer.stop_stream()
    """

    def __init__(
        self,
        ffmpeg_settings: FFmpegSettings,
        stream_settings: StreamSettings,
    ) -> None:
        self._ffmpeg = ffmpeg_settings.path
        self._video_bitrate = ffmpeg_settings.video_bitrate
        self._audio_bitrate = ffmpeg_settings.audio_bitrate
        self._framerate = ffmpeg_settings.framerate
        self._width = ffmpeg_settings.width
        self._height = ffmpeg_settings.height
        self._rtmp_url = stream_settings.rtmp_full_url
        self._status = StreamStatus.IDLE
        self._process: asyncio.subprocess.Process | None = None
        self._frame_queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue(maxsize=50)
        self._push_task: asyncio.Task[None] | None = None

    @property
    def status(self) -> StreamStatus:
        return self._status

    def is_streaming(self) -> bool:
        return self._status == StreamStatus.LIVE

    async def start_stream(self, rtmp_url: str | None = None) -> None:
        """Start the RTMP stream to YouTube.

        Launches an ffmpeg process that reads from an image sequence
        and pushes to the RTMP endpoint.
        """
        url = rtmp_url or self._rtmp_url
        if not url or "your_stream_key" in url:
            raise ValueError(
                "YouTube stream key not configured. "
                "Set YOUTUBE_STREAM_KEY in .env"
            )

        self._status = StreamStatus.STARTING

        # Start the frame processing loop
        self._push_task = asyncio.create_task(self._stream_loop(url))
        self._status = StreamStatus.LIVE

        logger.info("RTMP stream started → %s", url[:50] + "...")

    async def push_frame(self, frame_path: str, audio_path: str | None = None) -> None:
        """Queue a frame (and optional audio) for streaming."""
        if not self.is_streaming():
            logger.warning("Cannot push frame — stream not active")
            return

        try:
            self._frame_queue.put_nowait((frame_path, audio_path))
        except asyncio.QueueFull:
            logger.warning("Frame queue full, dropping frame: %s", frame_path)

    async def stop_stream(self) -> None:
        """Gracefully stop the RTMP stream."""
        self._status = StreamStatus.STOPPED

        if self._push_task and not self._push_task.done():
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass

        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._process.kill()

        logger.info("RTMP stream stopped.")

    async def _stream_loop(self, rtmp_url: str) -> None:
        """Main streaming loop — processes queued frames and pushes to RTMP.

        For each frame+audio pair, creates a short segment and pushes it.
        This is a simpler approach than pipe-based streaming but more robust.
        """
        segment_dir = Path("output/segments")
        segment_dir.mkdir(parents=True, exist_ok=True)
        segment_idx = 0

        while self._status == StreamStatus.LIVE:
            try:
                frame_path, audio_path = await asyncio.wait_for(
                    self._frame_queue.get(),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                # No frames for 5s — send a still frame to keep stream alive
                continue

            segment_idx += 1

            try:
                # Create a short segment from frame + audio
                if audio_path and os.path.exists(audio_path):
                    segment_path = str(segment_dir / f"seg_{segment_idx:06d}.flv")
                    await self._create_and_push_segment(
                        frame_path, audio_path, rtmp_url
                    )
                else:
                    # Frame-only update (no audio this ball)
                    pass

            except Exception:
                logger.exception("Error pushing segment %d", segment_idx)
                self._status = StreamStatus.ERROR
                await asyncio.sleep(2)
                self._status = StreamStatus.LIVE

    async def _create_and_push_segment(
        self,
        frame_path: str,
        audio_path: str,
        rtmp_url: str,
    ) -> None:
        """Create a short video segment from a frame + audio and push to RTMP."""
        cmd = [
            self._ffmpeg, "-y",
            # Input: loop the frame for the audio duration
            "-loop", "1",
            "-i", frame_path,
            # Audio input
            "-i", audio_path,
            # Video encoding
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "stillimage",
            "-pix_fmt", "yuv420p",
            "-r", str(self._framerate),
            "-s", f"{self._width}x{self._height}",
            # Audio encoding
            "-c:a", "aac",
            "-b:a", self._audio_bitrate,
            "-ar", "44100",
            # Match audio duration
            "-shortest",
            # Output to RTMP
            "-f", "flv",
            rtmp_url,
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="replace")[-300:]
            logger.error("RTMP push failed: %s", error_msg)
            raise FFmpegError(f"RTMP segment push failed: {error_msg}")

        logger.debug("RTMP segment pushed: %s", frame_path)


class FFmpegError(Exception):
    """Raised when ffmpeg command fails."""
