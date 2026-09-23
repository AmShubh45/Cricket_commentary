"""AWS Bedrock LLM provider for commentary generation.

Supports both Amazon Nova Micro (budget, ~$0.035/1M input tokens) and
Claude Haiku 4.5 (quality, ~$1/1M input tokens). The model is selected
via BEDROCK_MODEL_ID in settings.

Commentary is generated in Hindi with emotion tags. Each call is traced
via Langfuse for observability and cost tracking.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import boto3
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import LLMSettings
from cricket.domain.enums import EmotionTag, EventTier, EventType
from cricket.domain.models import CommentaryLine

logger = logging.getLogger(__name__)

# Cost per 1M tokens (USD) — updated for Aug 2026 pricing
_MODEL_COSTS: dict[str, dict[str, float]] = {
    "amazon.nova-micro-v1:0": {"input": 0.035, "output": 0.14},
    "anthropic.claude-3-5-haiku-20241022-v1:0": {"input": 1.0, "output": 5.0},
    "anthropic.claude-3-haiku-20240307-v1:0": {"input": 0.25, "output": 1.25},
}


class BedrockLLMProvider:
    """AWS Bedrock LLM provider implementing LLMProviderProtocol.

    Usage:
        provider = BedrockLLMProvider(settings)
        commentary = await provider.generate_commentary(prompt, context)
    """

    def __init__(self, settings: LLMSettings) -> None:
        self._model_id = settings.bedrock_model_id
        self._max_tokens = settings.bedrock_max_tokens
        self._temperature = settings.bedrock_temperature
        self._region = settings.aws_region

        self._client = boto3.client(
            "bedrock-runtime",
            region_name=self._region,
        )

        # Resolve cost rates for this model
        self._cost_rates = _MODEL_COSTS.get(
            self._model_id,
            {"input": 0.035, "output": 0.14},  # Default to Nova Micro rates
        )

        logger.info("Bedrock LLM initialized: model=%s, region=%s", self._model_id, self._region)

    async def generate_commentary(
        self,
        prompt: str,
        match_context: dict[str, Any],
        trace_id: str | None = None,
    ) -> CommentaryLine:
        """Generate original Hindi commentary via AWS Bedrock.

        The prompt is pre-built by the CommentaryOrchestrator with full
        match context. This method handles the API call, response parsing,
        and cost tracking.
        """
        system_prompt = self._build_system_prompt()
        full_prompt = self._build_full_prompt(prompt, match_context)

        try:
            response = self._invoke_model(system_prompt, full_prompt)

            # Parse response
            generated_text = self._extract_text(response)
            input_tokens = response.get("usage", {}).get("inputTokens", 0)
            output_tokens = response.get("usage", {}).get("outputTokens", 0)
            cost = self.estimate_cost(input_tokens, output_tokens)

            # Extract emotion from context or default to EXCITED for major events
            emotion = EmotionTag(match_context.get("emotion", EmotionTag.EXCITED))
            event_type = EventType(match_context.get("event_type", EventType.BOUNDARY))

            commentary = CommentaryLine(
                text=generated_text.strip(),
                emotion=emotion,
                event_type=event_type,
                tier=EventTier.MAJOR,
                source="llm",
                match_id=match_context.get("match_id", ""),
                over_display=match_context.get("over_display", ""),
                timestamp=datetime.utcnow(),
                llm_input_tokens=input_tokens,
                llm_output_tokens=output_tokens,
                llm_cost_usd=cost,
            )

            logger.info(
                "Bedrock generated commentary: %d in / %d out tokens, $%.6f — %s",
                input_tokens,
                output_tokens,
                cost,
                generated_text[:80],
            )

            return commentary

        except Exception:
            logger.exception("Bedrock commentary generation failed")
            raise

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
    )
    def _invoke_model(self, system_prompt: str, user_prompt: str) -> dict:
        """Invoke the Bedrock model with retry logic.

        Note: This is synchronous because boto3's bedrock-runtime client
        doesn't have async support. In production, wrap in asyncio.to_thread().
        """
        # Build request body based on model type
        if "anthropic" in self._model_id:
            body = self._build_anthropic_body(system_prompt, user_prompt)
        else:
            body = self._build_nova_body(system_prompt, user_prompt)

        response = self._client.invoke_model(
            modelId=self._model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )

        return json.loads(response["body"].read())

    def _build_anthropic_body(self, system_prompt: str, user_prompt: str) -> dict:
        """Build request body for Anthropic Claude models."""
        return {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_prompt},
            ],
        }

    def _build_nova_body(self, system_prompt: str, user_prompt: str) -> dict:
        """Build request body for Amazon Nova models."""
        return {
            "messages": [
                {"role": "user", "content": [{"text": f"{system_prompt}\n\n{user_prompt}"}]},
            ],
            "inferenceConfig": {
                "maxTokens": self._max_tokens,
                "temperature": self._temperature,
            },
        }

    def _extract_text(self, response: dict) -> str:
        """Extract generated text from model response (handles both Anthropic and Nova formats)."""
        # Anthropic Claude format
        if "content" in response and isinstance(response["content"], list):
            for block in response["content"]:
                if block.get("type") == "text":
                    return block["text"]

        # Amazon Nova format
        if "output" in response:
            output = response["output"]
            if isinstance(output, dict) and "message" in output:
                content = output["message"].get("content", [])
                if content and isinstance(content, list):
                    return content[0].get("text", "")

        # Fallback: try common response formats
        if "completion" in response:
            return response["completion"]

        logger.warning("Unexpected Bedrock response format: %s", list(response.keys()))
        return ""

    async def healthcheck(self) -> bool:
        """Verify Bedrock connectivity by listing foundation models."""
        try:
            client = boto3.client("bedrock", region_name=self._region)
            client.list_foundation_models(maxResults=1)
            return True
        except Exception:
            logger.exception("Bedrock healthcheck failed")
            return False

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost in USD for the given token counts."""
        input_cost = (input_tokens / 1_000_000) * self._cost_rates["input"]
        output_cost = (output_tokens / 1_000_000) * self._cost_rates["output"]
        return round(input_cost + output_cost, 8)

    @staticmethod
    def _build_system_prompt() -> str:
        """System prompt for Hindi cricket commentary generation.

        This prompt establishes the persona, style, and constraints for
        the LLM. It's designed to produce natural-sounding Hindi commentary
        that avoids repetition and sounds like a real Indian commentator.
        """
        return """तुम एक अनुभवी हिंदी क्रिकेट कमेंटेटर हो। तुम्हारी शैली आकाशवाणी और Hindi commentary की परंपरा से प्रेरित है।

नियम:
1. केवल हिंदी में बोलो (Devanagari script)। अंग्रेज़ी शब्द तभी इस्तेमाल करो जब वो क्रिकेट के standard terms हों (boundary, six, LBW, etc.)
2. हर commentary 1-3 वाक्यों में हो। बहुत लंबा मत बोलो।
3. बल्लेबाज़ और गेंदबाज़ के नाम ज़रूर बोलो।
4. मैच की स्थिति (score, overs, target) का reference दो।
5. भावनात्मक और जोशीला बोलो — जैसे असली कमेंटेटर बोलता है।
6. हर बार अलग तरीके से बोलो — repetition से बचो।
7. कभी-कभी मुहावरे या कहावतें इस्तेमाल करो।
8. कोई ग़लत stats या facts मत बनाओ — सिर्फ़ दिए गए context का इस्तेमाल करो।

शैली के उदाहरण:
- चौका: "क्या शॉट है! कोहली ने कवर ड्राइव से गेंद को बाउंड्री तक पहुंचा दिया!"
- छक्का: "और ये गई हवा में! स्टेडियम में सन्नाटा छा गया!"  
- विकेट: "आउट! बिल्कुल सटीक गेंद! गिल्लियां बिखर गईं!"
- डॉट बॉल: "अच्छी गेंद, बल्लेबाज़ खेल नहीं पाया।"
"""

    @staticmethod
    def _build_full_prompt(prompt: str, context: dict) -> str:
        """Build the complete prompt with match context for the LLM."""
        context_str = "\n".join(f"- {k}: {v}" for k, v in context.items() if k != "emotion")
        return f"""मैच संदर्भ (Context):
{context_str}

घटना (Event):
{prompt}

इस घटना के लिए 1-3 वाक्यों में हिंदी कमेंट्री लिखो:"""
