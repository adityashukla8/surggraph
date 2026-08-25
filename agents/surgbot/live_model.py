"""Live API model for SurgBot's root agent — a NEW, parallel function to
tools/gemini_model.py::new_agent_model, not an edit to it.

Two real reasons this can't reuse GlobalGemini directly: (1) GlobalGemini's
api_client override forces location="global" via a *cached_property*
override, but ADK's live connect path (google_llm.py::Gemini._live_api_client)
is a SEPARATE cached_property that builds its own genai.Client from scratch
and does NOT consult api_client at all — confirmed by direct source read and
by a real failed Day-1 spike deploy (the model name change took effect but
the live path kept hitting us-central1 regardless of the api_client
override); (2) "don't touch existing setup" — this file exists so SurgBot's
Live model choice can never regress tools/gemini_model.py's working, tested
text path.

THE CORRECT FIX, CONFIRMED (Day-1 spike): both api_client AND
_live_api_client read a real, documented Pydantic field on the base Gemini
class — `client_kwargs: Optional[dict[str, Any]]`, "Extra arguments to pass
to the google.genai.Client constructor" — and both merge it into their
Client(**kwargs) call. Setting client_kwargs={"vertexai": True, "project":
..., "location": ...} on a plain `Gemini(...)` instance (no subclass needed)
fixes both paths at once, which a cached_property override on only one of
them cannot do.

MODEL CHOICE, DISCLOSED: no Gemini 3.5+ model exists on Live API with
function-calling support today. The only model actually named "3.5" on Live
API, gemini-3.5-live-translate-preview, is confirmed (via its own model
card) to be a narrow speech-to-speech translation model with no tool use, no
system instructions, and no general conversational capability. Presented to
the user directly; their explicit decision: use a Live API model anyway for
voice/turn-taking only, on the condition that this is persistently and
specifically disclosed in the product itself — see agents/surgbot/root_agent.py
and the frontend's disclosure banner. This is not a caveat bolted on after
the fact; it's why ToolUseDisclosure.model_id/api_surface are required
fields (agents/surgbot/schema.py), not optional ones.

MODEL NAME AND LOCATION, REVISED (real user report: "sounds like an ugly bot
voice"). The original choice here, gemini-live-2.5-flash@global, is Google's
"half-cascade" architecture (separate ASR -> LLM -> TTS pipeline stitched
together) — Google's own docs contrast this explicitly with "native audio"
models, marketed specifically for "natural, realistic-sounding speech."
Nothing in the original Day-1 spike ever compared voice quality; the model
was chosen purely to fix 404s and confirm tool-calling worked.

Switching to a native-audio model risked a real, documented Google-
acknowledged issue: native-audio models can go silent after a function
response instead of narrating it (confirmed via Google's own developer
forum — a fix for this is on their roadmap, not shipped). A synthetic
"ping" tool test (no real content to narrate) reproduced silence 100% of
the time on BOTH this model and the native-audio candidate, which could
have been mistaken for a real regression. It wasn't: re-tested against the
REAL deployed root_agent with a REAL, substantive tool
(list_accessible_cases, real Firestore data) and the native-audio model
reliably spoke a full natural narration every time ("I found 102 cases
available for review. Which one would you like to start with?"), with real
audio (inline_data) throughout. The synthetic test's silence was an
artifact of giving the model nothing worth saying, not a model defect.

CURRENT MODEL, CONFIRMED (this session, via a real Agent Runtime redeploy +
bidi_stream_query smoke test against actual root_agent.py tools, not a raw
API toy): `gemini-live-2.5-flash-preview-native-audio-09-2025`. It resolves
at Vertex AI location `us-central1` — NOT `global` (confirmed via a sweep of
candidate native-audio model IDs across seven Vertex locations; this one was
the only candidate that resolved anywhere in that sweep, and only at
us-central1). This is a DIFFERENT location requirement than the previous
half-cascade model, which only resolved at `global` — the two models are not
interchangeable location-wise. Superseded resource (gemini-live-2.5-flash) was
projects/518946358970/locations/us-central1/reasoningEngines/581674086686523392,
torn down after the native-audio deployment was confirmed working.

VOICE SELECTION, MOVED HERE (real user report: changing SURGBOT_LIVE_VOICE
had zero audible effect, tried across three different voice names). Root
cause found by pulling Google's own official ADK sample from GitHub
(google/adk-python, contributing/samples/live/live_bidi_streaming_multi_agent/
agent.py) — it sets `speech_config` directly on the `Gemini` model
constructor, never via a per-connection RunConfig. Traced why this matters in
the installed SDK (google/adk/models/google_llm.py::Gemini.connect()):

    if self.speech_config is not None:
        llm_request.live_connect_config.speech_config = self.speech_config

The MODEL's own speech_config, if set, is what actually reaches the wire.
services/surgbot_service/main.py was previously setting speech_config only on
a per-connection run_config sent over bidi_stream_query — a path that should
still work per a plain code trace (nothing nulls it out when the model itself
has none set), but real empirical evidence says it wasn't taking effect
end-to-end through the EXPERIMENTAL Agent-Runtime relay. Matching Google's own
demonstrated, trusted pattern here instead of continuing to debug an
unverified secondary path. Trade-off, worth knowing: voice is now baked into
the deployed agent — changing it requires a redeploy
(scripts/deploy_surgbot_agent.py), not just a new WebSocket session.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from google.adk.models import Gemini
from google.genai import types

load_dotenv()

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
SURGBOT_LIVE_MODEL = os.environ.get("SURGBOT_LIVE_MODEL", "gemini-live-2.5-flash-preview-native-audio-09-2025")
SURGBOT_LIVE_LOCATION = os.environ.get("SURGBOT_LIVE_LOCATION", "us-central1")
# No numeric speaking-rate control exists anywhere in the Live API (checked
# directly against SpeechConfig/VoiceConfig/GenerationConfig — no rate/speed/
# pitch field, unlike the older Cloud Text-to-Speech API's
# AudioConfig.speakingRate) — pace is bundled into voice persona, not a
# separate dial. Real full voice-name list (30 total), per Google's own docs:
# Zephyr, Puck, Charon, Kore, Fenrir, Leda, Orus, Aoede, Callirrhoe, Autonoe,
# Enceladus, Iapetus, Umbriel, Algieba, Despina, Erinome, Algenib, Rasalgethi,
# Laomedeia, Achernar, Alnilam, Schedar, Gacrux, Pulcherrima, Achird,
# Zubenelgenubi, Vindemiatrix, Sadachbia, Sadaltager, Sulafat.
SURGBOT_LIVE_VOICE = os.environ.get("SURGBOT_LIVE_VOICE", "Kore")


def new_live_agent_model(model_name: str = SURGBOT_LIVE_MODEL) -> Gemini:
    return Gemini(
        model=model_name,
        client_kwargs={
            "vertexai": True,
            "project": PROJECT_ID,
            "location": SURGBOT_LIVE_LOCATION,
        },
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=SURGBOT_LIVE_VOICE)
            )
        ),
    )
