"""Application configuration using Pydantic Settings.

All configuration is loaded from environment variables (12-Factor App principle).
Provider selection is done here — the Factory reads these settings to instantiate
the correct provider implementations.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from cricket.domain.enums import DataFeedProvider, LLMProvider, TTSProvider


class DatabaseSettings(BaseSettings):
    """PostgreSQL connection settings."""

    model_config = SettingsConfigDict(env_prefix="POSTGRES_")

    host: str = "localhost"
    port: int = 5432
    db: str = "cricket_commentary"
    user: str = "cricket"
    password: str = "changeme_in_production"

    @property
    def dsn(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


class RedisSettings(BaseSettings):
    """Redis connection settings."""

    url: str = "redis://localhost:6379/0"
    match_state_db: int = Field(1, alias="REDIS_MATCH_STATE_DB")
    celery_broker_db: int = Field(2, alias="REDIS_CELERY_BROKER_DB")

    model_config = SettingsConfigDict(env_prefix="REDIS_", populate_by_name=True)


class CelerySettings(BaseSettings):
    """Celery worker configuration."""

    model_config = SettingsConfigDict(env_prefix="CELERY_")

    broker_url: str = "redis://localhost:6379/2"
    result_backend: str = "redis://localhost:6379/3"


class DataFeedSettings(BaseSettings):
    """Cricket data feed provider configuration."""

    model_config = SettingsConfigDict(env_prefix="")

    provider: DataFeedProvider = Field(
        DataFeedProvider.CRICKETDATA,
        alias="DATA_FEED_PROVIDER",
    )
    poll_interval_seconds: int = Field(30, alias="DATA_POLL_INTERVAL_SECONDS")

    # CricketData.org
    cricketdata_api_key: str = Field("", alias="CRICKETDATA_API_KEY")
    cricketdata_base_url: str = Field(
        "https://api.cricapi.com/v1",
        alias="CRICKETDATA_BASE_URL",
    )

    # EntitySport (upgrade path)
    entitysport_api_key: str = Field("", alias="ENTITYSPORT_API_KEY")
    entitysport_base_url: str = Field(
        "https://rest.entitysport.com/v2",
        alias="ENTITYSPORT_BASE_URL",
    )


class TTSSettings(BaseSettings):
    """Text-to-Speech provider configuration."""

    model_config = SettingsConfigDict(env_prefix="TTS_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    provider: TTSProvider = Field(TTSProvider.EDGE_TTS, alias="TTS_PROVIDER")
    voice_id: str = "hi-IN-SwaraNeural"
    output_format: str = "mp3"
    sample_rate: int = 24000

    # ElevenLabs (upgrade path)
    elevenlabs_api_key: str = Field("", alias="ELEVENLABS_API_KEY")
    elevenlabs_voice_id: str = Field("", alias="ELEVENLABS_VOICE_ID")
    elevenlabs_model_id: str = Field("eleven_flash_v2_5", alias="ELEVENLABS_MODEL_ID")

    # Sarvam AI — free natural Hindi TTS (https://www.sarvam.ai/)
    # Set TTS_PROVIDER=sarvam and SARVAM_API_KEY=<your_key> in .env
    sarvam_api_key: str = Field("", alias="SARVAM_API_KEY")


class LLMSettings(BaseSettings):
    """LLM provider configuration for commentary generation."""

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    provider: LLMProvider = Field(LLMProvider.BEDROCK, alias="LLM_PROVIDER")

    # AWS Bedrock
    aws_region: str = Field("us-east-1", alias="AWS_REGION")
    bedrock_model_id: str = Field(
        "amazon.nova-micro-v1:0",
        alias="BEDROCK_MODEL_ID",
    )
    bedrock_max_tokens: int = Field(300, alias="BEDROCK_MAX_TOKENS")
    bedrock_temperature: float = Field(0.8, alias="BEDROCK_TEMPERATURE")

    # Ollama (free fallback)
    ollama_base_url: str = Field("http://localhost:11434", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field("llama3.1:8b", alias="OLLAMA_MODEL")


class LangfuseSettings(BaseSettings):
    """Langfuse observability configuration."""

    model_config = SettingsConfigDict(env_prefix="LANGFUSE_")

    public_key: str = ""
    secret_key: str = ""
    host: str = "https://cloud.langfuse.com"
    enabled: bool = True


class StreamSettings(BaseSettings):
    """YouTube RTMP streaming configuration."""

    model_config = SettingsConfigDict(env_prefix="")

    youtube_rtmp_url: str = Field(
        "rtmp://a.rtmp.youtube.com/live2",
        alias="YOUTUBE_RTMP_URL",
    )
    youtube_stream_key: str = Field("", alias="YOUTUBE_STREAM_KEY")

    @property
    def rtmp_full_url(self) -> str:
        return f"{self.youtube_rtmp_url}/{self.youtube_stream_key}"


class FFmpegSettings(BaseSettings):
    """ffmpeg rendering configuration."""

    model_config = SettingsConfigDict(env_prefix="FFMPEG_")

    path: str = "ffmpeg"
    video_bitrate: str = "2500k"
    audio_bitrate: str = "128k"
    framerate: int = 30
    resolution: str = "1920x1080"

    @property
    def width(self) -> int:
        return int(self.resolution.split("x")[0])

    @property
    def height(self) -> int:
        return int(self.resolution.split("x")[1])


class GraphicsSettings(BaseSettings):
    """Scorecard graphics rendering configuration."""

    model_config = SettingsConfigDict(env_prefix="")

    scorecard_template_path: str = Field(
        "templates/scorecard/index.html",
        alias="SCORECARD_TEMPLATE_PATH",
    )
    playwright_screenshot_width: int = Field(1920, alias="PLAYWRIGHT_SCREENSHOT_WIDTH")
    playwright_screenshot_height: int = Field(1080, alias="PLAYWRIGHT_SCREENSHOT_HEIGHT")


class CostTrackingSettings(BaseSettings):
    """Cost tracking and budget alert configuration."""

    model_config = SettingsConfigDict(env_prefix="COST_TRACKING_")

    enabled: bool = True
    monthly_budget_alert_inr: float = Field(1000.0, alias="MONTHLY_BUDGET_ALERT_INR")


class AppSettings(BaseSettings):
    """Root settings — aggregates all subsystem configurations.

    Usage:
        settings = AppSettings()  # Loads from environment
        print(settings.tts.provider)  # → TTSProvider.EDGE_TTS
    """

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"
    debug: bool = True
    log_level: str = "DEBUG"

    # Subsystem configs — loaded independently so each can have its own env prefix
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    celery: CelerySettings = Field(default_factory=CelerySettings)
    data_feed: DataFeedSettings = Field(default_factory=DataFeedSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    stream: StreamSettings = Field(default_factory=StreamSettings)
    ffmpeg: FFmpegSettings = Field(default_factory=FFmpegSettings)
    graphics: GraphicsSettings = Field(default_factory=GraphicsSettings)
    cost_tracking: CostTrackingSettings = Field(default_factory=CostTrackingSettings)

    @property
    def is_production(self) -> bool:
        return self.env == "production"


# Singleton-ish access pattern — instantiated once at app startup
_settings: AppSettings | None = None


def get_settings() -> AppSettings:
    """Get the application settings singleton.

    Settings are loaded from environment variables on first access.
    In tests, you can override by setting _settings directly.
    """
    global _settings  # noqa: PLW0603
    if _settings is None:
        _settings = AppSettings()
    return _settings
