# Cricket Commentary Automation

Automated Hindi cricket commentary for YouTube live streaming. Generates real-time ball-by-ball commentary with TTS audio and scorecard graphics.

## Quick Start

### 1. Prerequisites
- Python 3.11+
- Docker & Docker Compose (for Redis + PostgreSQL)
- ffmpeg (system install)

### 2. Setup

```bash
# Clone and enter the project
cd cricket-commentary

# Copy environment config
cp .env.example .env
# Edit .env with your API keys (CRICKETDATA_API_KEY at minimum)

# Start infrastructure (PostgreSQL + Redis)
docker compose up -d postgres redis

# Install Python dependencies
pip install -e ".[dev]"

# Install Playwright browser
playwright install chromium
```

### 3. Test the pipeline (no API key needed)

```bash
# Run the simulated replay — generates commentary for 18 balls
python scripts/replay_match.py --simulate --delay 1.0

# Check output
ls output/audio/
```

### 4. Run with real data

```bash
# Start the API server
uvicorn cricket.main:app --reload

# List live matches
curl http://localhost:8000/api/v1/matches/live

# Start commentary for a match
curl -X POST http://localhost:8000/api/v1/matches/start \
  -H "Content-Type: application/json" \
  -d '{"match_id": "YOUR_MATCH_ID", "poll_interval": 30}'
```

## Architecture

```
Data Feed → Delta Detector → Event Classifier → Commentary Engine → TTS → Graphics → ffmpeg → RTMP
                                   ↓                    ↓
                              Template (85%)         LLM (15%)
                              (free, instant)    (Bedrock, traced)
```

## Provider Swapping

Change providers via `.env` — zero code changes:

```bash
# TTS: edge_tts (free) → elevenlabs ($5/mo)
TTS_PROVIDER=elevenlabs

# LLM: Nova Micro ($0.035/1M) → Claude Haiku ($1/1M)  
BEDROCK_MODEL_ID=anthropic.claude-3-5-haiku-20241022-v1:0

# Data: cricketdata ($6/mo) → entitysport ($150/mo)
DATA_FEED_PROVIDER=entitysport
```

## Project Structure

```
src/cricket/
├── domain/          # Pure business models (BallEvent, MatchState, enums)
├── providers/       # External service adapters (Strategy Pattern)
│   ├── data_feed/   # CricketData, EntitySport
│   ├── tts/         # Edge TTS, ElevenLabs
│   └── llm/         # Bedrock, Ollama
├── services/        # Core business logic
│   ├── classifier/  # Event classification (routes to template vs LLM)
│   ├── commentary/  # Template engine + LLM commentary + orchestrator
│   └── pipeline/    # Main pipeline orchestrator
├── infra/           # Redis state store, provider factory, cost tracking
├── workers/         # Celery tasks and beat schedule
└── api/             # FastAPI routes (health, matches, stream control)
```

## Running Tests

```bash
pytest tests/ -v
```
