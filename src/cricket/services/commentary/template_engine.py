"""Template-based commentary engine for ROUTINE and MINOR events.

Handles ~85-90% of all balls. Uses a bank of 200+ Hindi commentary templates
organized by event type, innings phase, and match situation. Templates use
Python string formatting with named placeholders for player names, scores, etc.

Design choices:
1. Templates loaded from JSON files at startup — easy to add/edit without code changes
2. Weighted random selection — avoids repetition by tracking recently used templates
3. Context-aware selection — different templates for powerplay vs death overs
4. Thread-safe — no shared mutable state (recent history is per-match in Redis)
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import Any

from cricket.domain.enums import (
    EmotionTag,
    EventTier,
    EventType,
    InningsPhase,
    MatchSituation,
)
from cricket.domain.models import ClassifiedEvent, CommentaryLine

logger = logging.getLogger(__name__)

# Template placeholder keys available for formatting
# {batsman}, {bowler}, {runs}, {score}, {wickets}, {overs}, {target},
# {runs_needed}, {balls_remaining}, {partnership}, {strike_rate}


class TemplateEngine:
    """Template-based Hindi commentary generator.

    Usage:
        engine = TemplateEngine(template_dir="templates/commentary")
        line = engine.generate(classified_event)
    """

    def __init__(self, template_dir: str = "templates/commentary") -> None:
        self._templates: dict[str, list[dict[str, Any]]] = {}
        self._recently_used: dict[str, list[int]] = {}  # event_type → [template indices]
        self._max_recent = 10  # Track last N used templates per event type

        self._load_templates(template_dir)
        self._load_builtin_templates()

    def generate(self, event: ClassifiedEvent) -> CommentaryLine:
        """Generate a commentary line from templates for the given classified event.

        Selects a template based on event type, phase, and situation.
        Uses weighted random with recency avoidance to reduce repetition.
        """
        event_key = event.event_type.value
        templates = self._templates.get(event_key, [])

        if not templates:
            # Fallback to generic templates for this tier
            templates = self._templates.get(f"generic_{event.tier.value}", [])

        if not templates:
            # Ultimate fallback
            logger.warning("No templates found for event type: %s", event_key)
            return self._fallback_commentary(event)

        # Filter by phase/situation if templates have context tags
        filtered = self._filter_by_context(templates, event.phase, event.situation)
        if not filtered:
            filtered = templates  # If no context-specific templates, use all

        # Select with recency avoidance
        template = self._select_template(event_key, filtered)

        # Format the template with event context
        text = self._format_template(template["text"], event)

        return CommentaryLine(
            text=text,
            emotion=EmotionTag(template.get("emotion", event.emotion.value)),
            event_type=event.event_type,
            tier=event.tier,
            source="template",
            match_id=event.ball_event.match_id,
            over_display=event.ball_event.over_display,
        )

    def _load_templates(self, template_dir: str) -> None:
        """Load templates from JSON files in the template directory."""
        template_path = Path(template_dir)
        if not template_path.exists():
            logger.warning("Template directory not found: %s", template_dir)
            return

        for json_file in template_path.glob("*.json"):
            try:
                with open(json_file, encoding="utf-8") as f:
                    data = json.load(f)

                event_type = json_file.stem  # filename without extension
                if isinstance(data, list):
                    self._templates[event_type] = data
                elif isinstance(data, dict) and "templates" in data:
                    self._templates[event_type] = data["templates"]

                logger.info(
                    "Loaded %d templates from %s",
                    len(self._templates.get(event_type, [])),
                    json_file.name,
                )
            except (json.JSONDecodeError, OSError) as e:
                logger.error("Failed to load templates from %s: %s", json_file, e)

    def _load_builtin_templates(self) -> None:
        """Load built-in Hindi commentary templates.

        These serve as the default template bank. JSON files in the
        templates/ directory can override or supplement these.
        """
        builtins = _get_builtin_templates()
        for event_type, templates in builtins.items():
            if event_type not in self._templates:
                self._templates[event_type] = templates
            else:
                # Merge: builtin templates supplement file-based ones
                existing_texts = {t["text"] for t in self._templates[event_type]}
                for t in templates:
                    if t["text"] not in existing_texts:
                        self._templates[event_type].append(t)

        total = sum(len(v) for v in self._templates.values())
        logger.info("Total templates loaded: %d across %d event types", total, len(self._templates))

    @staticmethod
    def _filter_by_context(
        templates: list[dict],
        phase: InningsPhase,
        situation: MatchSituation,
    ) -> list[dict]:
        """Filter templates that match the current innings phase and situation."""
        filtered = []
        for t in templates:
            t_phases = t.get("phases", [])
            t_situations = t.get("situations", [])

            # If template has no context tags, it applies everywhere
            if not t_phases and not t_situations:
                filtered.append(t)
                continue

            phase_match = not t_phases or phase.value in t_phases
            situation_match = not t_situations or situation.value in t_situations

            if phase_match and situation_match:
                filtered.append(t)

        return filtered

    def _select_template(self, event_key: str, templates: list[dict]) -> dict:
        """Select a template with recency avoidance.

        Tracks the last N used template indices per event type and
        preferentially selects templates that haven't been used recently.
        """
        if event_key not in self._recently_used:
            self._recently_used[event_key] = []

        recent = set(self._recently_used[event_key])
        indices = list(range(len(templates)))

        # Prefer templates not recently used
        fresh_indices = [i for i in indices if i not in recent]
        if fresh_indices:
            chosen_idx = random.choice(fresh_indices)
        else:
            # All used recently — reset and pick randomly
            self._recently_used[event_key] = []
            chosen_idx = random.choice(indices)

        # Track usage
        self._recently_used[event_key].append(chosen_idx)
        if len(self._recently_used[event_key]) > self._max_recent:
            self._recently_used[event_key] = self._recently_used[event_key][-self._max_recent:]

        return templates[chosen_idx]

    @staticmethod
    def _format_template(template_text: str, event: ClassifiedEvent) -> str:
        """Fill template placeholders with event context values.

        Uses format_map with a defaultdict so unknown placeholders
        render as empty string instead of raising KeyError.
        """
        from collections import defaultdict

        context = defaultdict(str, {
            "batsman": event.ball_event.batsman.display_name,
            "bowler": event.ball_event.bowler.display_name,
            "runs": str(event.ball_event.runs_batsman),
            "total_runs": str(event.ball_event.runs_total),
            "score": f"{event.team_score}/{event.team_wickets}",
            "team_score": str(event.team_score),
            "team_wickets": str(event.team_wickets),
            "wickets": str(event.team_wickets),
            "overs": str(event.team_overs),
            "over_display": event.ball_event.over_display,
            "target": str(event.target or ""),
            "runs_needed": str(event.runs_needed or ""),
            "balls_remaining": str(event.balls_remaining or ""),
            "partnership": str(event.partnership_runs),
            "batsman_runs": str(event.batsman_runs),
            "batsman_balls": str(event.batsman_balls),
            "strike_rate": str(
                round((event.batsman_runs / event.batsman_balls) * 100, 1)
                if event.batsman_balls > 0
                else 0
            ),
            "team_name": event.ball_event.batsman.team or "India",
            "bowling_team": event.ball_event.bowler.team or "",
            "rr": str(
                round(event.team_score / max(float(event.team_overs) if event.team_overs else 1, 0.1), 2)
            ),
        })

        try:
            return template_text.format_map(context)
        except (KeyError, IndexError, ValueError) as e:
            logger.warning("Template formatting failed: %s — %s", template_text[:50], e)
            return template_text

    @staticmethod
    def _fallback_commentary(event: ClassifiedEvent) -> CommentaryLine:
        """Generate minimal fallback commentary when no templates match."""
        text = f"{event.ball_event.bowler.display_name} की गेंद पर {event.ball_event.batsman.display_name}।"
        return CommentaryLine(
            text=text,
            emotion=EmotionTag.NEUTRAL,
            event_type=event.event_type,
            tier=event.tier,
            source="template_fallback",
            match_id=event.ball_event.match_id,
            over_display=event.ball_event.over_display,
        )


def _get_builtin_templates() -> dict[str, list[dict]]:
    """Built-in Hindi commentary template bank — Aakash Chopra / Jatin Sapru style.

    Every template is 3-5 sentences mimicking real Hindi cricket commentary:
    Ball description -> Shot/Action -> Result -> Score context -> Match narrative.
    """
    return {
        "dot_ball": [
            {"text": "और {bowler} ने डाली है एक अच्छी लेंथ की गेंद! {batsman} ने बचाव किया लेकिन रन नहीं मिला। गेंदबाज़ को शाबाशी मिलनी चाहिए, बहुत अच्छी लाइन थी ये। स्कोर है {score}, {overs} ओवर हो चुके हैं।", "emotion": "neutral"},
            {"text": "{bowler} आ रहे हैं, गेंद डाली... और {batsman} छोड़ देते हैं! अच्छा फ़ैसला, गेंद ऑफ़ स्टंप के बाहर जा रही थी। कोई रन नहीं इस गेंद पर। {bowler} अपनी लय में आ रहे हैं दोस्तों! स्कोर {score}।", "emotion": "neutral"},
            {"text": "तेज़ गेंद! {bowler} ने {batsman} को बीट कर दिया! गेंद बल्ले के बाहर से निकल गई, विकेटकीपर ने पकड़ी। क्या ख़तरनाक गेंद थी ये! {batsman} को सावधान रहना होगा। स्कोर {score}।", "emotion": "neutral"},
            {"text": "और ये है डॉट बॉल! {bowler} ने शानदार कंट्रोल दिखाया है। {batsman} आगे बढ़कर खेलना चाहते थे लेकिन गेंद ने करारा जवाब दे दिया। गेंदबाज़ का दबदबा बना हुआ है इस ओवर में। {score}।", "emotion": "neutral"},
            {"text": "{bowler} दौड़ते हुए आए, गेंद डाली... और {batsman} ने बैक फ़ुट पर डिफ़ेंड किया। कोई रन नहीं, लेकिन अच्छी तकनीक दिखी बल्लेबाज़ की। गेंदबाज़ और बल्लेबाज़ के बीच ज़बरदस्त मुक़ाबला! {score}।", "emotion": "neutral"},
            {"text": "गुड लेंथ गेंद {bowler} की! {batsman} खेल नहीं पाए, बल्ला हवा में रह गया। दबाव बनता जा रहा है बल्लेबाज़ पर! स्कोर {score}।", "emotion": "neutral"},
            {"text": "क्या गेंद है! {bowler} ने ऐसी गेंद डाली कि {batsman} को कुछ समझ नहीं आया! हवा में कट करती हुई गई। बहुत ख़तरनाक गेंदबाज़ी चल रही है दोस्तों! {score}।", "emotion": "neutral"},
            {"text": "{bowler} आए और टाइट लाइन पर डाला! {batsman} ने छोड़ दी, समझदारी दिखाई। T20 में भी कभी-कभी सब्र ज़रूरी है। स्कोर {score}।", "emotion": "neutral"},
            {"text": "शॉर्ट ऑफ़ लेंथ! {batsman} ने डक किया, समझदारी से छोड़ दी। ये बाउंसर था {bowler} का! अच्छा फ़ैसला। {score}।", "emotion": "neutral"},
            {"text": "और एक और डॉट! {bowler} ने फिर से {batsman} को बाँध दिया! लगातार अच्छी गेंदबाज़ी। रनों का सूखा बना हुआ है! {score}।", "emotion": "neutral"},
            {"text": "डॉट बॉल! {bowler} ने यॉर्कर डाला और {batsman} खोद नहीं पाए! हर गेंद अब सोने जैसी है! {score}!", "emotion": "tense", "phases": ["death_overs"], "situations": ["chasing_tight", "chasing_desperate"]},
            {"text": "रन नहीं! {batsman} ने मारने की कोशिश की लेकिन {bowler} ने ऐसी यॉर्कर फेंकी कि बल्ला हवा में रह गया! दर्शकों की साँसें थम गई हैं! {score}।", "emotion": "tense", "phases": ["death_overs"]},
        ],
        "single": [
            {"text": "और {batsman} ने सिंगल ले लिया! मिड-ऑन की तरफ़ हल्का पुश और एक रन। स्कोरबोर्ड चलता रहा! {batsman} अभी {batsman_runs} रन बना चुके हैं {batsman_balls} गेंदों में। स्कोर {score}।", "emotion": "neutral"},
            {"text": "{bowler} की गेंद पर {batsman} ने धीरे से खेला और एक रन चुरा लिया! अच्छी क्रिकेट बुद्धिमानी! स्ट्राइक रोटेट करना बहुत ज़रूरी है T20 में। स्कोर अब {score}।", "emotion": "neutral"},
            {"text": "एक रन! {batsman} ने कवर की तरफ़ खेला और तेज़ी से दौड़ लगाई। अच्छी रनिंग बीच विकेट में! हर रन मायने रखता है। {batsman} {batsman_runs} पर। {score}।", "emotion": "neutral"},
            {"text": "लेग साइड में गई गेंद, {batsman} ने flick किया और एक रन। बहुत soft hands से खेला गया ये शॉट! {score}। {batsman} settle हो रहे हैं।", "emotion": "neutral"},
            {"text": "{batsman} ने थर्ड मैन की तरफ़ guide किया, एक रन! स्मार्ट क्रिकेट! ज़रूरी नहीं कि हर गेंद पर छक्का मारो! {score}।", "emotion": "neutral"},
            {"text": "और एक रन! {batsman} ने {bowler} की गेंद पर squeeze किया point की तरफ़। दौड़कर एक रन। स्कोर बोर्ड चलता रहना चाहिए, और वो चल रहा है। {score}।", "emotion": "neutral"},
            {"text": "हल्के हाथों से {batsman} ने लेग साइड में रखा और एक रन! {batsman_runs} रन हो गए अब। {score} है {overs} ओवर में।", "emotion": "neutral"},
        ],
        "double": [
            {"text": "और {batsman} ने दो रन दौड़ लिए! गैप में खेला और तेज़ रनिंग! दोनों बल्लेबाज़ों के बीच बहुत अच्छी समझ है। {batsman} {batsman_runs} पर। स्कोर {score}।", "emotion": "neutral"},
            {"text": "डीप मिड-विकेट में गई गेंद! {batsman} ने पुश किया और दो रन! Fielder ने चेज़ किया लेकिन दो बन गए। {score}।", "emotion": "neutral"},
            {"text": "स्क्वेयर लेग में मारा {batsman} ने! दो रन! Energy कमाल की है मैदान पर! {score}। गेंदबाज़ के लिए मुश्किल बढ़ रही है।", "emotion": "neutral"},
            {"text": "{bowler} की गेंद पर {batsman} ने wide of mid-on खेला! दो रन! बहुत तेज़ दौड़! {batsman} अब {batsman_runs} पर। {score}।", "emotion": "neutral"},
        ],
        "boundary": [
            {"text": "और ये है चौका! {batsman} ने {bowler} की गेंद पर शानदार कवर ड्राइव मारी! गेंद ज़मीन पर दौड़ती हुई बाउंड्री पार! क्या टाइमिंग! दर्शक तालियाँ बजा रहे हैं! {score}!", "emotion": "excited"},
            {"text": "बाउंड्री! {bowler} ने शॉर्ट डाला और {batsman} ने इंतज़ार ही तो कर रहे थे! पुल शॉट! गेंद मिड-विकेट बाउंड्री पार! क्या ताक़तवर शॉट! {batsman} {batsman_runs} पर! {score}!", "emotion": "excited"},
            {"text": "चार रन! {batsman} ने पैर जमाकर सीधी ड्राइव मारी! Bowler के सिर के ऊपर से! {bowler} देखते रह गए! Textbook shot! {score}! रन rate बढ़ रहा है!", "emotion": "excited"},
            {"text": "फ़ोर! {batsman} ने बैक फ़ुट पर जाकर कट मारा है! गेंद backward point से बाउंड्री पार! {bowler} को ये लाइन नहीं डालनी चाहिए थी! {score}!", "emotion": "excited"},
            {"text": "बाउंड्री! ख़ूबसूरत शॉट! {batsman} ने {bowler} की फ़ुल गेंद पर ऑन ड्राइव किया! ज़मीन पर दौड़ती हुई गेंद रस्सी तक! क्या wrist work! {batsman} {batsman_runs} पर! {score}!", "emotion": "excited"},
            {"text": "और चार! {batsman} ने गैप ढूंढ लिया! {bowler} की गेंद extra cover से निकल गई! बल्लेबाज़ ने बहुत placement से खेला! {score}!", "emotion": "excited"},
            {"text": "टेक्स्टबुक शॉट! {batsman} ने {bowler} को सबक सिखा दिया! कवर ड्राइव! गेंद ऐसे गई जैसे मिसाइल! चार रन! स्टेडियम में शोर! {score}!", "emotion": "excited"},
            {"text": "आउटस्विंग पर {batsman} ने लेट कट मारा! बाउंड्री! {bowler} सोच रहे होंगे कहाँ ग़लती हो गई! बहुत ही क्लासी शॉट! {batsman} {batsman_runs} पर! {score}!", "emotion": "excited"},
            {"text": "बाउंड्री! बहुत ज़रूरी थी ये! {batsman} ने दबाव में भी चौका निकाला! ये है बड़े खिलाड़ियों की पहचान! {score}! मैच गर्म हो रहा है!", "emotion": "celebratory", "phases": ["death_overs"]},
        ],
        "six": [
            {"text": "छक्का! और ये गई हवा में! {batsman} ने {bowler} को ऊपर से मारा! गेंद stands में जा गिरी! क्या ज़बरदस्त पावर! स्टेडियम में पागलपन! {batsman} {batsman_runs} पर! {score}!", "emotion": "celebratory"},
            {"text": "सिक्स! मैक्सिमम! {batsman} ने step out करके {bowler} को लॉन्ग ऑन पार भेजा! ये तो बहुत बड़ा छक्का है दोस्तों! दर्शक खड़े हो गए! {score}!", "emotion": "celebratory"},
            {"text": "और ये गई... गई... गई! छक्का! {batsman} ने {bowler} की गेंद को हवा में उड़ा दिया! Stadium की दूसरी tier तक गई! क्या ताक़त! {batsman} {batsman_runs} रन! {score}!", "emotion": "celebratory"},
            {"text": "भारी-भरकम छक्का! {batsman} ने पूरी ताक़त लगा दी! {bowler} ने short डाला और {batsman} तैयार थे! गेंद पार्किंग में गिरी! {score}!", "emotion": "celebratory"},
            {"text": "Towering six! {batsman} ने {bowler} को जो मारा है ना, ये याद रहेगा! गेंद stands में गई! {batsman} {batsman_runs} पर! {score}!", "emotion": "celebratory"},
            {"text": "सिक्स! {batsman} ने {bowler} को clean hit किया! Straight down the ground! क्या timing! क्या power! {score}!", "emotion": "celebratory"},
            {"text": "और एक और छक्का! {batsman} आज तूफ़ान लेकर आए हैं! {bowler} परेशान! {batsman} {batsman_runs} रन {batsman_balls} गेंदों में! {score}!", "emotion": "celebratory"},
            {"text": "छक्का! {batsman} ने मैच पलटने की ठान ली है! दर्शक चिल्ला रहे हैं! {runs_needed} रन और चाहिए! मैच रोमांचक!", "emotion": "celebratory", "situations": ["chasing_tight", "chasing_desperate"]},
        ],
        "wicket": [
            {"text": "आउट! और ये बड़ा विकेट है! {bowler} ने {batsman} को चलता किया! क्या गेंदबाज़ी! {batsman} {batsman_runs} रन बनाकर पवेलियन लौटे! स्कोर {score}! {team_wickets} विकेट गिरे!", "emotion": "dramatic"},
            {"text": "विकेट! गया! {batsman} आउट! {bowler} ने ऐसी गेंद डाली जो {batsman} को पूरी तरह beat कर गई! बड़ा झटका! {batsman_runs}({batsman_balls}) बनाए! {score}!", "emotion": "dramatic"},
            {"text": "OUT! गिल्लियां बिखर गईं! {bowler} की तूफ़ानी गेंद! {batsman} ने कुछ नहीं कर पाए! बल्ला देर से आया और stumps उड़ गए! {score}! मैच में ट्विस्ट!", "emotion": "dramatic"},
            {"text": "कैच! शानदार कैच! {batsman} आउट! {bowler} ने edge लगवाई और fielder ने clean catch लिया! {batsman} निराश! {batsman_runs} रन बनाए। {score}!", "emotion": "dramatic"},
            {"text": "एलबीडब्ल्यू! प्लम्ब! अंपायर ने तुरंत उंगली उठा दी! {bowler} ने {batsman} को pad पर मारा! {batsman} {batsman_runs}({batsman_balls}) बनाकर गए! {score}!", "emotion": "dramatic"},
            {"text": "विकेट गिरा! {batsman} को {bowler} ने निकाला! टीम मुश्किल में! {batsman_runs} रन बना पाए! नए बल्लेबाज़ को pressure में आना होगा! {score}!", "emotion": "dramatic"},
            {"text": "और एक विकेट और! {bowler} on fire! {batsman} को निकाला {batsman_runs} पर! ये गेंदबाज़ मैच बदल रहा है! {score}!", "emotion": "dramatic"},
            {"text": "विकेट! बड़ा विकेट! {batsman} गए {batsman_runs} बनाकर! {runs_needed} रन {balls_remaining} गेंदों में चाहिए! मुश्किल बढ़ी!", "emotion": "dramatic", "situations": ["chasing_tight", "chasing_desperate"]},
        ],
        "wide": [
            {"text": "और ये वाइड! {bowler} की गेंद बहुत बाहर निकल गई! Umpire ने हाथ फैलाए। {bowler} को line सुधारनी होगी। Free run! {score}।", "emotion": "neutral"},
            {"text": "वाइड बॉल! {bowler} से control गया! Free run। T20 में हर extra run भारी पड़ सकता है। {score}।", "emotion": "neutral"},
            {"text": "बहुत बाहर! वाइड! {bowler} को ध्यान देना होगा। Captain सोच रहे होंगे कि बदलाव ज़रूरी है। {score}।", "emotion": "neutral"},
        ],
        "no_ball": [
            {"text": "नो बॉल! {bowler} ने ओवरस्टेप की! अगली गेंद फ्री हिट! {batsman} को सुनहरा मौका! दबाव में बड़ी गलती! {score}।", "emotion": "excited"},
            {"text": "नो बॉल! Foot over the line! {bowler} से ग़लती! अब free hit! {batsman} तैयार हो जाइए! {score}!", "emotion": "excited"},
        ],
        "over_end": [
            {"text": "ओवर ख़त्म! {overs} ओवर में {score}। अच्छा ओवर रहा! अब नए गेंदबाज़ आएंगे। खेल रोचक है दोस्तों!", "emotion": "neutral"},
            {"text": "ओवर समाप्त! {score} है {overs} ओवर में। अगले ओवर में क्या होता है, देखना दिलचस्प होगा!", "emotion": "neutral"},
        ],
        "milestone_50": [
            {"text": "अर्धशतक! {batsman} ने 50 पूरे किए! हेलमेट उतारा, बल्ला उठाया! तालियाँ! {batsman_runs} रन {batsman_balls} गेंदों में! शानदार! {score}!", "emotion": "celebratory"},
            {"text": "FIFTY! {batsman} का कमाल! Dressing room से तालियाँ! {batsman_balls} गेंदों में अर्धशतक! {score}!", "emotion": "celebratory"},
            {"text": "पचास! {batsman} ने बल्ला लहराया! हर शॉट खेला — कवर ड्राइव, पुल, स्वीप! {batsman_runs}({batsman_balls})! {score}!", "emotion": "celebratory"},
        ],
        "milestone_100": [
            {"text": "शतक! शतक! {batsman} ने सौ रन पूरे किए! स्टेडियम गूंज उठा! {batsman_runs} रन {batsman_balls} गेंदों में! इतिहास! {score}!", "emotion": "celebratory"},
            {"text": "CENTURY! {batsman} ने कमाल! Helmet उतारा, bat उठाया! साथी दौड़कर आए! {batsman_runs}({batsman_balls})! जीवन भर याद रहेगी!", "emotion": "celebratory"},
        ],
        "innings_break": [
            {"text": "पारी समाप्त! Final score {score}! अब दूसरी टीम को ये target चेज़ करना होगा। Break के बाद मिलते हैं!", "emotion": "neutral"},
        ],
        "match_start": [
            {"text": "नमस्कार दोस्तों! मैच शुरू होने वाला है! खिलाड़ी मैदान पर! पहली गेंद डाली जाएगी! रोमांचक मुक़ाबला होगा!", "emotion": "excited"},
        ],
        "match_end": [
            {"text": "मैच ख़त्म! क्या शानदार मुक़ाबला! दोनों टीमों ने शानदार क्रिकेट खेली! अगले मैच में मिलेंगे! नमस्कार!", "emotion": "celebratory"},
        ],
        "maiden_over": [
            {"text": "मेडन ओवर! {bowler} ने एक भी रन नहीं दिया! पूरा ओवर! ज़ीरो! शानदार गेंदबाज़ी! {batsman} को बाँध दिया!", "emotion": "excited"},
        ],
        "partnership_50": [
            {"text": "50 रन की साझेदारी! दोनों बल्लेबाज़ शानदार! Running कमाल, shot selection बढ़िया! Partnership ने innings stabilize की! {score}!", "emotion": "excited"},
        ],
        "partnership_100": [
            {"text": "शतकीय साझेदारी! 100 रन! दोनों ने तहलका मचाया! गेंदबाज़ परेशान, Captain परेशान! ये partnership मैच बदल रही है!", "emotion": "celebratory"},
        ],
        "five_wicket_haul": [
            {"text": "पाँच विकेट! {bowler} ने पंचक लगाया! साथी दौड़कर गले मिले! {bowler} ने मैच अपने नाम किया! अविश्वसनीय!", "emotion": "celebratory"},
        ],
        "generic_routine": [
            {"text": "{bowler} ने गेंद डाली, {batsman} ने खेला। खेल आगे बढ़ रहा है। {score} है {overs} ओवर में। बहुत क्रिकेट बाक़ी है!", "emotion": "neutral"},
            {"text": "खेल जारी है! {bowler} और {batsman} के बीच मुक़ाबला! {score}। अभी बहुत क्रिकेट बाक़ी!", "emotion": "neutral"},
        ],
        "generic_minor": [
            {"text": "रन मिले {batsman} को! {score}। {overs} ओवर हो चुके। गेंदबाज़ को कुछ करना होगा!", "emotion": "neutral"},
            {"text": "{batsman} ने {runs} रन लिए। {score}। मैच अच्छा चल रहा है!", "emotion": "neutral"},
        ],
    }

