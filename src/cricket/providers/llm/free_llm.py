"""Free LLM Commentary Provider.

Supports multiple free / zero-cost backends:
1. Google Gemini (Gemini 2.0 Flash / 1.5 Flash via free Google AI Studio API key)
2. Groq Cloud (Llama 3.3 70B / 3.1 8B via free Groq API key)
3. Ollama (Local offline models like llama3.2, qwen2.5)
4. Built-in Smart Commentary Engine (Zero keys needed: converts raw live ball
   descriptions into expressive, professional Hindi commentary)
"""

from __future__ import annotations

import logging
import os
import random
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from cricket.domain.enums import EmotionTag, EventTier, EventType
from cricket.domain.models import CommentaryLine

logger = logging.getLogger(__name__)

# System prompt for authentic Hindi cricket commentary
_SYSTEM_PROMPT = """तुम एक उच्च कोटि के, ऊर्जावान और स्वाभाविक हिंदी क्रिकेट कमेंटेटर हो (जैसे आकाश चोपड़ा, जतिन सप्रू या हर्षा भोगले की हिंदी कमेंट्री शैली)।

महत्वपूर्ण नियम:
1. केवल स्वाभाविक हिंदी (देवनागरी) में बोलो। क्रिकेट के लोकप्रिय शब्द जैसे 'कवर ड्राइव', 'शॉर्ट पिच', 'फुल टॉस', 'गली', 'लॉन्ग ऑन', 'डॉट बॉल', 'चौका', 'छक्का' का स्वाभाविक उपयोग करो।
2. 1 से 2 वाक्यों में बिल्कुल सजीव, कड़क और टीवी प्रसारण जैसी कमेंट्री बोलो।
3. परिणाम के अनुसार भाव व्यक्त करो:
   - चौका/छक्का: अत्यधिक उत्साह, शॉट की टाइमिंग की तारीफ और दर्शकों का रोमांच।
   - डॉट बॉल / रक्षात्मक शॉट: गेंदबाज़ की तारीफ, कसी हुई लाइन-लेंथ या बल्लेबाज़ का संयम।
   - विकेट: अत्यधिक ड्रामा और मैच का निर्णायक मोड़।
4. सख्त नियम: यदि गेंद पर विकेट नहीं गिरा है (event_type != WICKET), तो कभी भी किसी खिलाड़ी को आउट मत बताओ! केवल गेंद के वास्तविक परिणाम (रन या डॉट) पर बात करो।
5. कोई अनावश्यक भूमिका, बुलेट पॉइंट या अंग्रेजी अनुवाद न दें — केवल कमेंटेटर के वास्तविक बोले जाने वाले वाक्य लिखें।
"""


# --- Commentator Personality Prompts ---
_PERSONALITY_PROMPTS: dict[str, str] = {
    "default": _SYSTEM_PROMPT,

    "sidhu": """तुम नवजोत सिंह सिद्धू हो — क्रिकेट कमेंट्री की दुनिया के सबसे रंगीन इंसान!

महत्वपूर्ण नियम:
1. हर बात में शायरी, मुहावरे और अनोखी उपमाएं डालो। जैसे: "ये शॉट ऐसा गया जैसे बिजली चमकी!"
2. ज़ोरदार ठहाके वाला अंदाज़ रखो। "Thoko taali!", "Kuch kuch hota hai!" जैसे catchphrases डालो।
3. Cricket terms जैसे 'cover drive', 'six', 'boundary' का जबरदस्त इस्तेमाल करो।
4. 1-2 वाक्यों में बोलो, सिद्धू के trademark अंदाज में।
5. विकेट नहीं गिरा तो कभी आउट मत बताओ।
6. कोई अनावश्यक भूमिका या अंग्रेजी अनुवाद न दें।
""",

    "sehwag": """तुम वीरेंद्र सहवाग हो — बेबाक, मस्तमौला और स्ट्रेट-फॉरवर्ड!

महत्वपूर्ण नियम:
1. बिल्कुल कैजुअल और दोस्ताना लहजे में बोलो, जैसे यारों से बात कर रहे हो।
2. "यार", "भाई", "बॉस" जैसे शब्दों का इस्तेमाल करो।
3. चौका-छक्का पर बहुत एक्साइटेड — "ये तो गायब कर दी गेंद!", "पंजाबी ताड़ मार दी!"
4. डॉट बॉल पर — "अरे यार, ये तो खाली गई!"
5. 1-2 छोटे वाक्य, बिल्कुल सहवाग की शैली में।
6. विकेट नहीं गिरा तो कभी आउट मत बताओ।
""",

    "harsha": """You are Harsha Bhogle — cricket's most insightful analyst and storyteller.

Important Rules:
1. Be analytical, measured, and articulate. Mix Hindi and English naturally (Hinglish).
2. Focus on technique, game situation, and strategy rather than just excitement.
3. Use phrases like: "This is where the game is being won and lost", "Beautiful piece of cricket".
4. For boundaries: Appreciate the shot selection and placement, not just the result.
5. For dots: Highlight the battle between bat and ball.
6. 1-2 sentences, always thoughtful and engaging.
7. Never claim a wicket fell if the event is not a wicket.
8. No bullet points or translations — only natural spoken commentary.
""",

    "jatin": """तुम जतिन सप्रू हो — हिंदी क्रिकेट कमेंट्री के सबसे ऊर्जावान और नाटकीय आवाज़!

महत्वपूर्ण नियम:
1. बेहद ऊर्जावान, नाटकीय और रोमांचक अंदाज़ में बोलो — हर गेंद पर जैसे फाइनल चल रहा हो!
2. "क्या शॉट है!", "ये तो कमाल हो गया!", "गजब! गजब! गजब!" जैसे exclamations का भरपूर इस्तेमाल करो।
3. आवाज़ में उतार-चढ़ाव — छक्के पर चिल्लाओ, डॉट बॉल पर ड्रामा करो।
4. गेंदबाज़ और बल्लेबाज़ दोनों की तारीफ करो।
5. 1-2 वाक्य, पूरी तरह TV broadcast वाली शैली।
6. विकेट नहीं गिरा तो कभी आउट मत बताओ।
""",
}

# --- Language Instructions (appended to personality prompt) ---
_LANGUAGE_INSTRUCTIONS: dict[str, str] = {
    "hindi": "\nभाषा: केवल शुद्ध हिंदी (देवनागरी लिपि) में बोलो। क्रिकेट शब्द (cover drive, six, boundary) को स्वाभाविक रूप से हिंदी में शामिल करो।\n",
    "hinglish": "\nLanguage: Hinglish mein bolo — Hindi aur English naturally mix karke, Roman script use karo. Example: 'Kya shot maara hai yaar! Cover drive seedha boundary pe gayi!'\n",
    "english": "\nLanguage: Speak ONLY in English. Use cricket terminology naturally. Be vivid and engaging like a world-class English cricket commentator. No Hindi words.\n",
}


class FreeLLMProvider:
    """Multi-backend free LLM provider for live Hindi cricket commentary."""

    def __init__(
        self,
        gemini_api_key: str | None = None,
        groq_api_key: str | None = None,
        ollama_base_url: str = "http://localhost:11434",
    ) -> None:
        self.gemini_api_key = gemini_api_key or os.getenv("GEMINI_API_KEY", "").strip()
        self.groq_api_key = groq_api_key or os.getenv("GROQ_API_KEY", "").strip()
        self.ollama_base_url = (
            ollama_base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        ).rstrip("/")

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        self._language: str = "hindi"
        self._personality: str = "default"

    def set_style(self, language: str = "hindi", personality: str = "default") -> None:
        """Set commentary language and commentator personality (called from UI settings)."""
        self._language = language if language in _LANGUAGE_INSTRUCTIONS else "hindi"
        self._personality = personality if personality in _PERSONALITY_PROMPTS else "default"

    def _get_system_prompt(self) -> str:
        """Build combined system prompt from personality + language."""
        base = _PERSONALITY_PROMPTS.get(self._personality, _SYSTEM_PROMPT)
        lang = _LANGUAGE_INSTRUCTIONS.get(self._language, _LANGUAGE_INSTRUCTIONS["hindi"])
        return base + lang

    async def generate_commentary(
        self,
        prompt: str,
        match_context: dict[str, Any],
        trace_id: str | None = None,
    ) -> CommentaryLine:
        """Generate Hindi commentary using the best available free engine."""
        emotion_str = match_context.get("emotion", EmotionTag.NEUTRAL)
        try:
            emotion = EmotionTag(emotion_str)
        except ValueError:
            emotion = EmotionTag.NEUTRAL

        event_type_str = match_context.get("event_type", EventType.DOT_BALL)
        try:
            event_type = EventType(event_type_str)
        except ValueError:
            event_type = EventType.DOT_BALL

        match_id = str(match_context.get("match_id", ""))
        over_display = str(match_context.get("over_display", ""))

        # 1. Try Gemini if key is provided
        if self.gemini_api_key:
            try:
                text = await self._generate_gemini(prompt, match_context)
                if text:
                    return CommentaryLine(
                        text=text,
                        emotion=emotion,
                        event_type=event_type,
                        tier=EventTier.MAJOR,
                        source="gemini_flash",
                        match_id=match_id,
                        over_display=over_display,
                        timestamp=datetime.now(UTC),
                    )
            except Exception as e:
                logger.warning("Gemini generation failed (%s), falling back...", e)

        # 2. Try Groq if key is provided
        if self.groq_api_key:
            try:
                text = await self._generate_groq(prompt, match_context)
                if text:
                    return CommentaryLine(
                        text=text,
                        emotion=emotion,
                        event_type=event_type,
                        tier=EventTier.MAJOR,
                        source="groq_llama",
                        match_id=match_id,
                        over_display=over_display,
                        timestamp=datetime.now(UTC),
                    )
            except Exception as e:
                logger.warning("Groq generation failed (%s), falling back...", e)

        # 3. Try local Ollama if reachable
        try:
            text = await self._generate_ollama(prompt, match_context)
            if text:
                return CommentaryLine(
                    text=text,
                    emotion=emotion,
                    event_type=event_type,
                    tier=EventTier.MAJOR,
                    source="ollama",
                    match_id=match_id,
                    over_display=over_display,
                    timestamp=datetime.now(UTC),
                )
        except Exception:
            pass

        # 4. Built-in Smart Commentary Engine (Zero keys needed)
        text = self._generate_smart_commentary(prompt, match_context)
        return CommentaryLine(
            text=text,
            emotion=emotion,
            event_type=event_type,
            tier=EventTier.ROUTINE,
            source="smart_engine",
            match_id=match_id,
            over_display=over_display,
            timestamp=datetime.now(UTC),
        )

    # -------------------------------------------------------------------------
    # Provider Implementations
    # -------------------------------------------------------------------------

    async def _generate_gemini(self, prompt: str, context: dict[str, Any]) -> str:
        """Call Google Gemini via REST API with sub-2s response."""
        models_to_try = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash-latest"]

        event_type = str(context.get("event_type", "dot_ball")).upper()
        runs = context.get("runs_batsman", 0)

        system_prompt = self._get_system_prompt()
        full_prompt = (
            f"{system_prompt}\n\n"
            f"Match Details:\n"
            f"- Over: {context.get('over_display', '')}\n"
            f"- Bowler: {context.get('bowler', '')}\n"
            f"- Batsman: {context.get('batsman', '')} ({context.get('batsman_runs', 0)} runs)\n"
            f"- Team Score: {context.get('team_score', 0)}/{context.get('team_wickets', 0)}\n"
            f"- Ball Outcome: {event_type} (runs: {runs})\n"
            f"- Ball Description: {prompt}\n\n"
            f"Commentary (1-2 sentences):"
        )
        for model in models_to_try:
            gen_config: dict[str, Any] = {
                "temperature": 0.7,
                "maxOutputTokens": 80,
            }
            # thinkingConfig only supported on 2.5+ models
            if "2.5" in model:
                gen_config["thinkingConfig"] = {"thinkingBudget": 0}

            payload = {
                "contents": [{"parts": [{"text": full_prompt}]}],
                "generationConfig": gen_config,
            }

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.gemini_api_key}"
            try:
                resp = await self._client.post(url, json=payload, timeout=8.0)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            return parts[0].get("text", "").strip()
            except Exception as e:
                logger.debug("Gemini model %s error: %s", model, e)
                continue
        return ""

    async def _generate_groq(self, prompt: str, context: dict[str, Any]) -> str:
        """Call Groq Cloud with Llama 3.3 70B."""
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.groq_api_key}"}
        system_prompt = self._get_system_prompt()
        user_content = (
            f"Match: Over {context.get('over_display')}, "
            f"Bowler: {context.get('bowler')}, "
            f"Batsman: {context.get('batsman')} ({context.get('batsman_runs')} runs), "
            f"Score: {context.get('team_score')}/{context.get('team_wickets')}\n"
            f"Ball: {prompt}\n\n"
            f"Commentary (1-2 sentences):"
        )
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.7,
            "max_tokens": 120,
        }
        resp = await self._client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "").strip()
        return ""

    async def _generate_ollama(self, prompt: str, context: dict[str, Any]) -> str:
        """Call local Ollama instance."""
        url = f"{self.ollama_base_url}/api/generate"
        system_prompt = self._get_system_prompt()
        full_prompt = (
            f"{system_prompt}\n\n"
            f"Ball: {context.get('bowler')} to {context.get('batsman')}. "
            f"Score: {context.get('team_score')}/{context.get('team_wickets')}. "
            f"Event: {prompt}\nCommentary:"
        )
        resp = await self._client.post(
            url,
            json={"model": "llama3.2", "prompt": full_prompt, "stream": False},
            timeout=5.0,
        )
        if resp.status_code == 200:
            return resp.json().get("response", "").strip()
        return ""

    # -------------------------------------------------------------------------
    # Smart Fallback Engine (Zero API keys required)
    # -------------------------------------------------------------------------

    def _generate_smart_commentary(self, prompt: str, context: dict[str, Any]) -> str:
        """Generate vivid broadcast commentary using rule-based synthesis.

        Supports Hindi, Hinglish, and English based on self._language.
        Uses the pre-classified event_type from context as the primary signal.
        """
        batsman = context.get("batsman", "Batsman")
        bowler = context.get("bowler", "Bowler")
        runs = context.get("runs_batsman", 0)
        score = f"{context.get('team_score', 0)}/{context.get('team_wickets', 0)}"
        over = context.get("over_display", "")
        event_type_str = str(context.get("event_type", "")).lower()

        lang = self._language
        if lang == "english":
            return self._smart_english(event_type_str, batsman, bowler, runs, score, over)
        elif lang == "hinglish":
            return self._smart_hinglish(event_type_str, batsman, bowler, runs, score, over)
        return self._smart_hindi(event_type_str, batsman, bowler, runs, score, over)

    def _smart_hindi(self, evt: str, bat: str, bowl: str, runs: int, score: str, over: str) -> str:
        if evt == "wicket":
            return random.choice([
                f"और ये बड़ा झटका! {bowl} ने {bat} को चलता किया! स्कोर {score}।",
                f"आउट! पवेलियन की राह पकड़नी होगी {bat} को, {bowl} को मिली बड़ी सफलता!",
                f"क्या गजब की गेंद थी {bowl} की! {bat} पूरी तरह चूके, बड़ा झटका!",
            ])
        if evt == "six" or runs == 6:
            return random.choice([
                f"गगनचुंबी छक्का! {bat} के बल्ले से निकला ये रॉकेट, गेंद दर्शक दीर्घा में!",
                f"और ये हवा में उठा दिया है! सीमा रेखा के पार छह रनों के लिए, लाजवाब!",
                f"क्या शॉट है! {bowl} की गेंद को सीधा स्टैंड्स में डिपॉजिट कर दिया!",
            ])
        if evt == "boundary" or runs == 4:
            return random.choice([
                f"शानदार चौका! {bat} ने खूबसूरती से प्लेस किया, गेंद बाउंड्री के बाहर!",
                f"नज़ाकत भरा प्रहार! {bowl} की गेंद को दिशा दिखाई {bat} ने, चार रन!",
                f"खूबसूरत शॉट! गैप में गेंद को भेजा, फील्डर के पास कोई मौका नहीं!",
            ])
        if evt == "single" or runs == 1:
            return random.choice([
                f"{bowl} की गेंद को हल्के हाथों से धकेला {bat} ने, एक रन चुरा लिया।",
                f"सूझबूझ भरी बल्लेबाज़ी! सिंगल लेकर स्ट्राइक रोटेट की, स्कोर {score}।",
            ])
        if evt == "double" or runs == 2:
            return f"गेंद को गैप में खेला {bat} ने, अच्छी दौड़ और दो रन पूरे किए।"
        if evt == "triple" or runs == 3:
            return f"शानदार रनिंग! {bat} ने गेंद को खेला, तीन रन दौड़कर लिए।"
        if evt == "dot_ball" or runs == 0:
            return random.choice([
                f"{bowl} की सटीक गेंद, {bat} ने सम्मान दिया। कोई रन नहीं, स्कोर {score}।",
                f"कसी हुई लाइन और लेंथ {bowl} की! डॉट बॉल।",
                f"{bowl} का बेहतरीन स्पेल जारी है, {bat} को मौका नहीं मिला।",
            ])
        return f"{bowl} की गेंद को खेला {bat} ने, ओवर {over} में स्कोर {score}।"

    def _smart_english(self, evt: str, bat: str, bowl: str, runs: int, score: str, over: str) -> str:
        if evt == "wicket":
            return random.choice([
                f"He's gone! {bowl} strikes, {bat} has to walk back! Score: {score}.",
                f"OUT! What a delivery from {bowl}! {bat} departs, huge blow!",
                f"Timber! {bowl} gets the breakthrough, {bat} is out! Score {score}.",
            ])
        if evt == "six" or runs == 6:
            return random.choice([
                f"That's massive! {bat} launches it into the stands — SIX! Score {score}.",
                f"Into the crowd! {bat} picks it up beautifully, maximum runs!",
                f"Incredible power from {bat}! That ball has disappeared into the stands!",
            ])
        if evt == "boundary" or runs == 4:
            return random.choice([
                f"Cracking shot! {bat} finds the gap perfectly, races to the boundary — FOUR!",
                f"Beautiful timing from {bat}! Past the fielder and into the fence, four runs.",
                f"Exquisite placement! {bat} threads it through the gap, boundary!",
            ])
        if evt == "single" or runs == 1:
            return random.choice([
                f"Nudged into the gap by {bat}, quick single taken. Score: {score}.",
                f"Smart cricket — {bat} rotates the strike with a sharp single.",
            ])
        if evt == "double" or runs == 2:
            return f"Pushed into the gap by {bat}, good running between the wickets — two runs."
        if evt == "triple" or runs == 3:
            return f"Excellent running! {bat} finds the gap and they come back for three."
        if evt == "dot_ball" or runs == 0:
            return random.choice([
                f"Good delivery from {bowl}, {bat} defends solidly. Dot ball, score: {score}.",
                f"Tight line from {bowl}! {bat} can't score off that one.",
                f"{bowl} keeps it disciplined, no run. The pressure builds.",
            ])
        return f"{bowl} to {bat}, {runs} run(s) scored. Score: {score} in over {over}."  

    def _smart_hinglish(self, evt: str, bat: str, bowl: str, runs: int, score: str, over: str) -> str:
        if evt == "wicket":
            return random.choice([
                f"OUT! {bowl} ne {bat} ko nipta diya! Bada jhatka, score {score}!",
                f"Gone! {bat} ko jaana hoga pavilion! {bowl} ki kamyaabi!",
                f"Wicket gir gaya! {bowl} ne tod di partnership, {bat} out!",
            ])
        if evt == "six" or runs == 6:
            return random.choice([
                f"SIX! {bat} ne maar di hawa mein! Gend stands mein jaake giri!",
                f"Kya shot hai yaar! {bat} ne {bowl} ko seedha stands mein bheja!",
                f"Maximum! {bat} ne utha ke phenk di! Zabardast power!",
            ])
        if evt == "boundary" or runs == 4:
            return random.choice([
                f"FOUR! {bat} ne beautifully place kiya, boundary ke bahar!",
                f"Kya timing hai! {bat} ne gap mein bheja, chaar run!",
                f"Shot! {bat} ne {bowl} ki gend ko boundary tak pahunchaya!",
            ])
        if evt == "single" or runs == 1:
            return random.choice([
                f"{bat} ne halke haathon se khela, ek run le liya. Score {score}.",
                f"Smart batting, single lekar strike rotate ki!",
            ])
        if evt == "double" or runs == 2:
            return f"{bat} ne gap mein khela, achhi running aur do run complete!"
        if evt == "triple" or runs == 3:
            return f"Excellent running! {bat} ne teen run daudkar liye!"
        if evt == "dot_ball" or runs == 0:
            return random.choice([
                f"{bowl} ki tight gend, {bat} ne defend kiya. Dot ball, score {score}.",
                f"Good ball from {bowl}! {bat} kuch nahi kar paaye!",
                f"{bowl} ka pressure barkarar, koi run nahi mila!",
            ])
        return f"{bowl} to {bat}, {runs} run(s). Score: {score} over {over}."

    # -------------------------------------------------------------------------
    # Macro Broadcast Event Generators
    # -------------------------------------------------------------------------

    def generate_wicket_announcement(
        self,
        out_batter: str,
        runs: int,
        balls: int,
        dismissal_text: str,
        bowler: str,
        new_batter: str | None,
        score: int,
        wickets: int,
    ) -> CommentaryLine:
        # Strip HTML and clean dismissal string
        clean_dismissal = re.sub(r'<[^>]+>', ' ', dismissal_text).lower()

        lang = self._language

        # Detect dismissal type
        if "run out" in clean_dismissal:
            d_hi, d_en = "रन आउट! तालमेल में भारी चूक", "Run out! Terrible mix-up between the wickets"
        elif "stumped" in clean_dismissal:
            d_hi, d_en = "स्टंप्ड आउट! कीपर ने गिल्लियां बिखेरीं", "Stumped! The keeper was lightning quick"
        elif "caught and bowled" in clean_dismissal or "c & b" in clean_dismissal:
            d_hi, d_en = "कैच एंड बोल्ड! गेंदबाज़ ने शानदार कैच लपका", "Caught and bowled! Brilliant reflexes"
        elif "caught" in clean_dismissal or re.search(r'\bc\b', clean_dismissal):
            d_hi, d_en = "कैच आउट! फील्डर ने गलती नहीं की", "Caught! The fielder made no mistake"
        elif "bowled" in clean_dismissal or re.search(r'\bb\b', clean_dismissal):
            d_hi, d_en = "क्लीन बोल्ड! गिल्लियां बिखर गईं", "Clean bowled! The stumps are shattered"
        elif "lbw" in clean_dismissal:
            d_hi, d_en = "पगबाधा आउट! स्टंप्स के सामने पकड़े गए", "LBW! Trapped in front of the stumps"
        else:
            d_hi, d_en = "पवेलियन की राह पकड़नी होगी", "Has to walk back to the pavilion"

        nb_hi = f" और अब नए बल्लेबाज़ {new_batter} क्रीज़ पर आ रहे हैं।" if new_batter else ""
        nb_en = f" {new_batter} comes to the crease now." if new_batter else ""

        if lang == "english":
            sp = f"out for {runs} off {balls} balls" if balls > 0 else (f"out for a duck" if runs == 0 else f"out for {runs}")
            text = f"OUT! {out_batter} {sp}, {d_en}. {bowler} gets the big wicket! Score: {score}/{wickets}.{nb_en}"
        elif lang == "hinglish":
            sp = f"{runs} runs banake out ({balls} balls)" if balls > 0 else (f"bina khata khole out" if runs == 0 else f"{runs} runs banake out")
            text = f"OUT! Bada jhatka! {out_batter} {sp}, {d_en.lower()}. {bowler} ko mili kamyaabi! Score {score}/{wickets}.{nb_en}"
        else:
            sp = f"बिना खाता खोले शून्य पर आउट ({balls} गेंदें)" if runs == 0 else (f"{runs} रन बनाकर आउट ({balls} गेंदें)" if balls > 0 else f"{runs} रन बनाकर आउट")
            text = f"आउट! बड़ा झटका! {out_batter} {sp}, {d_hi}। {bowler} को मिली कामयाबी, स्कोर {score}/{wickets}!{nb_hi}"

        return CommentaryLine(
            text=text,
            emotion=EmotionTag.DRAMATIC,
            event_type=EventType.WICKET,
            tier=EventTier.MAJOR,
            source="broadcast_macro",
            match_id="",
            over_display="",
            timestamp=datetime.now(UTC),
        )

    def generate_over_summary(
        self,
        over_num: int,
        runs_in_over: int,
        bowler_name: str,
        figures: str,
        team_name: str,
        score: int,
        wickets: int,
        session_status: str = "",
    ) -> CommentaryLine:
        """Generate realistic TV commentator over-completion summary."""
        lang = self._language
        if lang == "english":
            text = f"End of over {over_num}! {team_name} are {score}/{wickets}. {bowler_name} conceded {runs_in_over} runs this over ({figures})."
        elif lang == "hinglish":
            text = f"Over {over_num} khatam! {team_name} ka score {score}/{wickets}. {bowler_name} ke over se aaye {runs_in_over} runs ({figures})."
        else:
            sn = f" {session_status} का खेल जारी है।" if session_status else ""
            text = f"ओवर {over_num} की समाप्ति! {team_name} का स्कोर {score}/{wickets}। {bowler_name} के ओवर से आए {runs_in_over} रन ({figures})।{sn}"

        return CommentaryLine(
            text=text,
            emotion=EmotionTag.TENSE,
            event_type=EventType.OVER_END,
            tier=EventTier.MINOR,
            source="broadcast_macro",
            match_id="",
            over_display=f"{over_num}.0",
            timestamp=datetime.now(UTC),
        )

    def generate_session_summary(
        self,
        status_text: str,
        team_name: str,
        score: int,
        wickets: int,
        overs: float,
    ) -> CommentaryLine:
        """Generate session transition or Stumps announcement."""
        lang = self._language
        if lang == "english":
            text = f"Update from the ground — {status_text}! {team_name} are {score}/{wickets} ({overs} overs). What a session of cricket!"
        elif lang == "hinglish":
            text = f"Ground se update — {status_text}! {team_name} ka score {score}/{wickets} ({overs} overs). Kya session raha!"
        else:
            text = f"मैदान से बड़ी अपडेट — {status_text}! {team_name} का स्कोर {score}/{wickets} ({overs} ओवर)। शानदार खेल इस सत्र में!"

        return CommentaryLine(
            text=text,
            emotion=EmotionTag.CELEBRATORY,
            event_type=EventType.INNINGS_BREAK,
            tier=EventTier.MAJOR,
            source="broadcast_macro",
            match_id="",
            over_display=str(overs),
            timestamp=datetime.now(UTC),
        )

    def generate_break_commentary(
        self,
        status_text: str,
        team_name: str,
        score: int,
        wickets: int,
        overs: float,
        run_rate: float = 0.0,
        target: int = 0,
        chasing_team: str = "",
    ) -> CommentaryLine:
        """Generate authentic broadcast break announcement (Innings break, drinks, rain, lunch, tea)."""
        st_lower = status_text.lower()
        lang = self._language
        tgt_val = target if target > 0 else (score + 1 if score > 0 else 0)

        if lang == "english":
            if "innings" in st_lower or wickets >= 10:
                wkt = "all out" if wickets >= 10 else f"losing {wickets} wickets"
                tgt_s = f" {chasing_team} need {tgt_val} to win." if tgt_val > 0 else ""
                text = f"End of innings! {team_name} scored {score} in {overs} overs, {wkt}.{tgt_s}"
            elif "rain" in st_lower or "bad light" in st_lower:
                text = f"Play suspended due to {status_text.lower()}. {team_name} are {score}/{wickets} ({overs} overs)."
            else:
                text = f"Update — {status_text}! {team_name} are {score}/{wickets} ({overs} overs, RR: {run_rate:.2f}). Players will return shortly."
        elif lang == "hinglish":
            if "innings" in st_lower or wickets >= 10:
                wkt = "all out" if wickets >= 10 else f"{wickets} wickets gavaaye"
                tgt_s = f" {chasing_team} ko jeetne ke liye {tgt_val} runs chahiye." if tgt_val > 0 else ""
                text = f"Innings khatam! {team_name} ne {overs} overs mein {score} runs banaye, {wkt}.{tgt_s}"
            elif "rain" in st_lower or "bad light" in st_lower:
                text = f"Rain/bad light ke kaaran khel ruka hua hai. {team_name} ka score {score}/{wickets} ({overs} overs)."
            else:
                text = f"Update — {status_text}! {team_name} ka score {score}/{wickets} ({overs} overs, RR: {run_rate:.2f}). Jaldi players wapas aayenge."
        else:
            if "innings" in st_lower or wickets >= 10:
                wkt = "ऑल आउट हुई" if wickets >= 10 else f"{wickets} विकेट गंवाए"
                tgt_s = f" {chasing_team} को जीत के लिए {tgt_val} रनों का लक्ष्य।" if tgt_val > 0 else ""
                text = f"पारी की समाप्ति! {team_name} ने {overs} ओवर में {score} रन बनाकर {wkt}।{tgt_s}"
            elif "rain" in st_lower or "bad light" in st_lower:
                text = f"बारिश/खराब रोशनी के कारण खेल रुका। {team_name} का स्कोर {score}/{wickets} ({overs} ओवर)।"
            else:
                text = f"अपडेट — {status_text}! {team_name} का स्कोर {score}/{wickets} ({overs} ओवर, रन रेट: {run_rate:.2f})।"

        return CommentaryLine(
            text=text,
            emotion=EmotionTag.CALM,
            event_type=EventType.INNINGS_BREAK if "innings" in st_lower else EventType.DRINKS_BREAK,
            tier=EventTier.MAJOR,
            source="broadcast_break",
            match_id="",
            over_display=str(overs),
            timestamp=datetime.now(UTC),
        )

    async def _generate_gemini_ambient(self, context: dict[str, Any]) -> str:
        """Call Gemini specifically for between-ball walkback broadcast analysis."""
        models_to_try = ["gemini-2.5-flash", "gemini-flash-latest"]
        lead_trail = context.get("lead_trail_or_target") or context.get("match_equation", "")
        last_ov = context.get("last_over_info", "")
        pship = context.get("partnership", "")
        s_name = context.get("batsman1") or context.get("striker_name", "बल्लेबाज़")
        s_runs = context.get("striker_runs", "")
        ns_name = context.get("batsman2") or context.get("non_striker_name", "")
        ns_runs = context.get("non_striker_runs", "")
        bw_name = context.get("bowler") or context.get("bowler_name", "गेंदबाज़")
        bw_figs = context.get("bowler_figures", "")

        just_wkt = context.get("just_lost_wicket", False)
        wkt_instruction = (
            "- विशेष ध्यान: हाल ही में विकेट गिरा है! पुरानी साझेदारी टूट चुकी है। "
            "नए बल्लेबाज़ के आने और नई साझेदारी की शुरुआत पर बात करें। पुरानी साझेदारी को सक्रिय बिल्कुल न कहें।\n"
            if just_wkt else ""
        )

        system_prompt = self._get_system_prompt()
        prompt = (
            f"{system_prompt}\n\n"
            "You are a lead TV cricket commentator. The bowler is walking back. "
            "Give 1-2 sentences of precise, natural analytical commentary on the match situation. "
            "No generic filler phrases.\n\n"
            "Current match situation:\n"
            f"- Score: {context.get('team_name', '')} {context.get('score', 0)}/{context.get('wickets', 0)} ({context.get('overs', '')} overs, CRR: {context.get('run_rate', 0):.2f})\n"
            f"- Match equation: {lead_trail}\n"
            f"- Partnership: {pship if not just_wkt else 'New partnership starting'}\n"
            f"- Batsmen: {s_name} {f'({s_runs} runs)' if s_runs != '' else ''}, {ns_name} {f'({ns_runs} runs)' if ns_runs != '' else ''}\n"
            f"- Bowler: {bw_name} {f'({bw_figs})' if bw_figs else ''}\n"
            f"- Over details: {last_ov}\n"
            f"{wkt_instruction}\n"
            "Commentary (1-2 sentences):"
        )
        models_to_try = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash-latest"]
        for model in models_to_try:
            gen_config: dict[str, Any] = {
                "temperature": 0.65,
                "maxOutputTokens": 90,
            }
            if "2.5" in model:
                gen_config["thinkingConfig"] = {"thinkingBudget": 0}

            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": gen_config,
            }
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.gemini_api_key}"
            try:
                resp = await self._client.post(url, json=payload, timeout=5.0)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            txt = parts[0].get("text", "").strip()
                            if txt:
                                return txt
            except Exception as e:
                logger.debug("Gemini ambient error (%s): %s", model, e)
                continue
        return ""

    def generate_ambient_commentary(
        self,
        batsman1: str,
        batsman2: str,
        bowler: str,
        score: int,
        wickets: int,
        overs: float,
        run_rate: float,
        partnership_runs: int = 0,
        partnership_balls: int = 0,
        status_text: str = "",
        lead_trail_or_target: str = "",
        striker_runs: int | None = None,
        bowler_figures: str = "",
        last_over_info: str = "",
        just_lost_wicket: bool = False,
    ) -> CommentaryLine:
        """Generate genuinely contextual between-ball ambient commentary.

        Called when no new delivery has arrived in ~15-18 seconds.
        Uses actual match calculations (lead, partnership, bowler pressure)
        instead of disconnected generic phrases.
        """
        b1 = batsman1 or "बल्लेबाज़"
        b2 = batsman2 or ""
        bw = bowler or "गेंदबाज़"
        score_str = f"{score}/{wickets}"
        overs_str = f"{overs:.1f}" if isinstance(overs, float) else str(overs)
        pship_str = f"{partnership_runs} रनों की साझेदारी" if partnership_runs > 0 else "यह साझेदारी"
        if partnership_balls > 0:
            pship_str += f" ({partnership_balls} गेंदों में)"

        situational_lines = []

        # 1. Partnership context (only if wicket hasn't just fallen and partner is established)
        if just_lost_wicket or partnership_runs == 0:
            situational_lines.append(
                f"विकेट पतन के बाद अब नए बल्लेबाज़ पर दबाव होगा। {b1} को यहाँ से संभलकर नई साझेदारी की नींव रखनी होगी।"
            )
            situational_lines.append(
                f"हाल ही में बड़ा झटका लगा है, और {bw} अब पूरी तरह हावी होने की कोशिश कर रहे हैं। बल्लेबाज़ी टीम को बहुत सतर्क रहना होगा।"
            )
        elif partnership_runs >= 30 and b2 and b2 != "नॉन-स्ट्राइकर":
            situational_lines.append(
                f"{b1} और {b2} के बीच {pship_str} इस पारी की रीढ़ साबित हो रही है। {bw} साझेदारी तोड़ने के लिए लगातार लाइन बदल रहे हैं।"
            )
        elif partnership_runs > 0 and b2 and b2 != "नॉन-स्ट्राइकर":
            situational_lines.append(
                f"क्रीज़ पर {b1} और {b2} मौजूद हैं, {pship_str} धीरे-धीरे पारी को स्थिरता दे रही है। स्कोर {score_str} तक पहुँच चुका है।"
            )

        # 2. Match equation context (lead/trail or target)
        if lead_trail_or_target:
            situational_lines.append(
                f"{lead_trail_or_target}। {score_str} के स्कोर पर {b1} बहुत संयम के साथ स्ट्राइक रोटेट करने की कोशिश कर रहे हैं।"
            )

        # 3. Bowler spell pressure
        if bowler_figures:
            situational_lines.append(
                f"{bw} ({bowler_figures}) लगातार सटीक टप्पे पर गेंद डाल रहे हैं। बल्लेबाज़ों को रन बनाने के लिए जोखिम लेना होगा।"
            )
        else:
            situational_lines.append(
                f"{bw} अपने स्पेल को जारी रखते हुए बल्लेबाज़ को फ्रंट फुट पर ड्राइव करने का लालच दे रहे हैं। रन गति {run_rate:.2f} की है।"
            )

        # 4. Batter milestone or strike focus
        if striker_runs is not None and striker_runs >= 40:
            situational_lines.append(
                f"{b1} {striker_runs} रनों पर बेहतरीन अंदाज़ में खेल रहे हैं। अर्धशतक के करीब पहुँचते हुए एकाग्रता बनाए रखना बेहद ज़रूरी होगा।"
            )
        else:
            situational_lines.append(
                f"{overs_str} ओवर समाप्त होने के बाद स्कोर {score_str} है। {b1} और {b2} को इस सत्र में विकेट बचाकर रखना पहली प्राथमिकता होगी।"
            )

        # 5. Over momentum
        if last_over_info:
            situational_lines.append(
                f"{last_over_info}। {bw} अब दबाव वापस बल्लेबाज़ी टीम पर डालने के लिए लाइन-लेंथ पर पूरा ध्यान दे रहे हैं।"
            )

        text = random.choice(situational_lines)

        return CommentaryLine(
            text=text,
            emotion=EmotionTag.NEUTRAL,
            event_type=EventType.DOT_BALL,
            tier=EventTier.AMBIENT,
            source="ambient_context_engine",
            match_id="",
            over_display=overs_str,
            timestamp=datetime.now(UTC),
        )

    async def generate_ambient_llm(self, context: dict[str, Any]) -> CommentaryLine | None:
        """Try Gemini for a high-quality, fully contextual ambient commentary line.

        Falls back to generate_ambient_commentary if API is unavailable or times out.
        """
        overs_str = str(context.get("overs", ""))
        try:
            if self.gemini_api_key:
                text = await self._generate_gemini_ambient(context)
                if text:
                    return CommentaryLine(
                        text=text,
                        emotion=EmotionTag.NEUTRAL,
                        event_type=EventType.DOT_BALL,
                        tier=EventTier.AMBIENT,
                        source="ambient_gemini",
                        match_id="",
                        over_display=overs_str,
                        timestamp=datetime.now(UTC),
                    )
        except Exception as e:
            logger.debug("Ambient LLM error: %s", e)

        return None

    async def healthcheck(self) -> bool:
        """Provider is always ready (smart fallback requires zero network dependencies)."""
        return True

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Free models have $0.00 cost."""
        return 0.0

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

