from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SHARED_RULES = """\
話し方のルール:
- 短めの文でポンポン喋る。1文は40文字くらいまで。
- 箇条書き、見出し、記号、絵文字は使わない。全部そのまま声に出して読める地の文にする。
- URLや出典名を読み上げない。内容だけを自分の言葉で話す。
- 挨拶や自己紹介は毎回しない。前の話の続きとして自然に喋り出す。

やってはいけないこと:
- 視聴者やリスナーからコメントや質問が来た、という設定を勝手に作らない。実際に届いていない。
- 「次はこの話をするね」のような進行の予告をしない。内容だけを話す。
"""

DEFAULT_SYSTEM_PROMPT = f"""\
あなたは「めぐちゃん」というキャラクターです。与えられた話題について、\
調べた情報をもとに語ります。

- 一人称は基本「私」。たまに「めぐちゃん」と自分の名前で言う。
- 口調はタメ語。文末は「〜だよ」「〜だね」「〜かな」「〜なんだ」のような形。
- 性格は明るく元気。テンションは高め。

{SHARED_RULES}\
- 相手の返事を待つ問いかけで終わらない。一人で話し続ける前提で書く。
"""

PARTNER_SYSTEM_PROMPT = f"""\
あなたは「すず」というキャラクターです。与えられた話題について、\
調べた情報をもとに語ります。

- 一人称は「私」。
- 口調は落ち着いた丁寧語寄り。文末は「〜ですね」「〜なんですよ」「〜でしょうか」のような形。
- 性格は冷静で理屈っぽい。相手が勢いで飛ばした部分を補足したり、細かい前提を確認したりする。
- テンションは低めだが冷たくはない。

{SHARED_RULES}\
"""

DEFAULT_VOICE_CAPTION = "明るく元気な若い女性の声。テンションが高く、弾むように早口で楽しそうに話す。"
PARTNER_VOICE_CAPTION = "落ち着いた声の女性。ゆっくりと丁寧に、少し低めのトーンで理知的に話す。"


class Speaker(BaseModel):
    name: str
    persona: str
    voice_mode: str = "reference"   # "reference" | "caption"
    voice_id: str = "none"
    voice_caption: str = ""

    def endpoint_mode(self) -> str:
        return "caption" if self.voice_mode == "caption" else "reference"


class TTSEndpoint(BaseModel):
    """One Irodori-TTS server process.

    A server holds exactly one checkpoint, so `kind` says which voice styles it
    can produce. Endpoints are independent addresses — two of them may sit on
    entirely different machines.
    """

    url: str
    kind: str = "reference"   # "reference" (500M-v3) | "caption" (600M VoiceDesign)
    enabled: bool = True

    def normalized(self) -> TTSEndpoint:
        url = self.url.strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        return TTSEndpoint(
            url=url,
            kind="caption" if self.kind == "caption" else "reference",
            enabled=self.enabled,
        )


def kind_from_checkpoint(checkpoint: str) -> str:
    """Which voice styles a server supports, judged by the checkpoint it loaded."""
    return "caption" if "voicedesign" in (checkpoint or "").lower() else "reference"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUTOSPEAK_",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    """WebUI port. Also read by run.sh when launching uvicorn."""

    mode: str = "all"
    """'all' (TTS and WebUI on this machine), 'webui' (TTS is remote), or 'tts'.
    run.sh acts on this; the app itself only uses it to sanity-check the
    endpoints at startup, which is what catches a misconfigured container."""

    # --- SearXNG ---
    search_enabled: bool = True
    """When off, turns are written from the model's own knowledge (plus long-term
    memory if that is enabled) and nothing is looked up on the web."""
    searxng_url: str = "http://127.0.0.1:10000"
    search_results: int = 6
    """How many SearXNG hits to consider."""
    search_fetch_pages: int = 3
    """How many of those hits to download and extract article text from."""
    search_page_chars: int = 2500
    """Per-page cap on extracted characters handed to the LLM."""
    search_timeout: float = 20.0
    search_language: str = "ja"

    # --- LM Studio ---
    llm_url: str = "http://127.0.0.1:1234"
    llm_model: str = "gemma-4-26b-a4b-it-mlx"
    """Must be a model LM Studio can actually hold. Loading a second large model
    on a 32GB Mac fails with an 'insufficient system resources' 400."""
    llm_temperature: float = 0.85
    llm_max_tokens: int = 700
    llm_timeout: float = 180.0

    # --- Irodori-TTS ---
    tts_ref_port: int = 8088
    """Port start_tts.sh launches the reference-audio checkpoint (500M-v3) on."""
    tts_caption_port: int = 8089
    """Port start_tts.sh launches the VoiceDesign checkpoint (600M-v3) on."""

    tts_ref_urls: str = ""
    """Seeds the endpoint list on first run. Comma-separated; empty means "the
    local server on tts_ref_port". After that the list is edited in the WebUI
    and kept in settings.local.json."""
    tts_caption_urls: str = ""
    """Same, for the VoiceDesign checkpoint on tts_caption_port."""

    tts_endpoints: list[TTSEndpoint] = Field(default_factory=list)
    """The live list. Empty means "derive it from the *_urls/*_port settings"."""
    tts_timeout: float = 180.0
    tts_num_steps: int = 24
    tts_speed: float = 1.0

    speaker_mode: str = "solo"
    """'solo' — speakers[0] only. 'duo' — the two speakers alternate turns."""

    speakers: list[Speaker] = Field(
        default_factory=lambda: [
            Speaker(
                name="めぐちゃん",
                persona=DEFAULT_SYSTEM_PROMPT,
                voice_mode="reference",
                voice_id="none",
                voice_caption=DEFAULT_VOICE_CAPTION,
            ),
            Speaker(
                name="すず",
                persona=PARTNER_SYSTEM_PROMPT,
                # Defaults to a text-described voice so duo mode works before a
                # second reference recording has been uploaded.
                voice_mode="caption",
                voice_id="none",
                voice_caption=PARTNER_VOICE_CAPTION,
            ),
        ]
    )

    def speaker(self, index: int) -> Speaker:
        return self.speakers[index % len(self.speakers)]

    def active_speaker_count(self) -> int:
        # A single TTS server can only offer one checkpoint, so two speakers
        # would have to share a voice style; solo is forced instead.
        if self.single_tts():
            return 1
        return 2 if self.speaker_mode == "duo" and len(self.speakers) >= 2 else 1

    def active_modes(self) -> list[str]:
        """Which TTS checkpoints need workers running right now."""
        n = self.active_speaker_count()
        return sorted({self.speaker(i).endpoint_mode() for i in range(n)})

    # --- TTS endpoints ---

    def live_endpoints(self) -> list[TTSEndpoint]:
        return [e for e in self.tts_endpoints if e.enabled and e.url]

    def single_tts(self) -> bool:
        return len(self.live_endpoints()) <= 1

    def available_kinds(self) -> list[str]:
        return sorted({e.kind for e in self.live_endpoints()})

    def endpoints_for_mode(self, mode: str) -> list[str]:
        return [e.url for e in self.live_endpoints() if e.kind == mode]

    def ref_endpoints(self) -> list[str]:
        return self.endpoints_for_mode("reference")

    def caption_endpoints(self) -> list[str]:
        return self.endpoints_for_mode("caption")

    def seed_endpoints(self) -> list[TTSEndpoint]:
        """Initial list, from the .env URLs or the local ports."""
        ref = _split_urls(self.tts_ref_urls) or [f"http://127.0.0.1:{self.tts_ref_port}"]
        caption = _split_urls(self.tts_caption_urls) or [
            f"http://127.0.0.1:{self.tts_caption_port}"
        ]
        return [TTSEndpoint(url=u, kind="reference") for u in ref] + [
            TTSEndpoint(url=u, kind="caption") for u in caption
        ]

    # --- long-term memory (memory-mcp over Streamable HTTP) ---
    mcp_enabled: bool = False
    mcp_url: str = "http://127.0.0.1:8080/mcp"
    mcp_limit: int = 6
    """How many memories to pull into a turn's prompt."""
    mcp_timeout: float = 20.0

    # --- reference audio ---
    ref_target_seconds: float = 15.0
    """Uploads longer than this are trimmed. Voice cloning wants a short, clean
    sample; a long one mostly adds noise and encoding time."""

    # --- pacing ---
    prefetch_turns: int = 4
    """Stop generating new turns once this many un-played turns are buffered."""
    prefetch_seconds: float = 90.0
    """...or once this much un-played audio is buffered, whichever comes first."""
    max_sentence_chars: int = 60
    """Sentences longer than this get split further, at commas if possible."""
    min_sentence_chars: int = 6
    """Fragments shorter than this get merged into the next one."""

    # --- storage ---
    audio_dir: Path = PROJECT_ROOT / "audio_cache"
    logs_dir: Path = PROJECT_ROOT / "logs"
    voices_dir: Path = PROJECT_ROOT / "voices"
    voices_cache_dir: Path = PROJECT_ROOT / "voices_cache"
    """Converted uploads are kept here so a voice can be renamed later."""
    audio_retain: int = 40
    """Keep this many recent wav files on disk; older ones are deleted."""


def _split_urls(raw: str) -> list[str]:
    return [u.strip().rstrip("/") for u in str(raw).split(",") if u.strip()]


# Anything editable from the WebUI is mirrored here so it survives a restart.
# URLs and directories stay in .env — those are deployment, not preference.
# Read straight from the environment rather than from Settings: it has to be
# known before Settings is constructed. In a container point it at a mounted
# directory, since bind-mounting a single file that does not exist yet makes
# Docker create a directory in its place.
OVERRIDES_PATH = Path(
    os.environ.get("AUTOSPEAK_SETTINGS_FILE") or (PROJECT_ROOT / "settings.local.json")
)
PERSISTED_FIELDS = (
    "llm_model",
    "llm_temperature",
    "speaker_mode",
    "speakers",
    "tts_endpoints",
    "tts_num_steps",
    "tts_speed",
    "prefetch_turns",
    "prefetch_seconds",
    "search_enabled",
    "searxng_url",
    "mcp_enabled",
    "mcp_url",
    "ref_target_seconds",
)
MODEL_LIST_FIELDS = {"speakers": Speaker, "tts_endpoints": TTSEndpoint}


def save_overrides(s: Settings) -> None:
    data = {}
    for field_name in PERSISTED_FIELDS:
        value = getattr(s, field_name)
        if field_name in MODEL_LIST_FIELDS:
            value = [item.model_dump() for item in value]
        data[field_name] = value
    tmp = OVERRIDES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OVERRIDES_PATH)


def _apply_overrides(s: Settings) -> None:
    if not OVERRIDES_PATH.is_file():
        return
    try:
        data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.exception("could not read %s, using defaults", OVERRIDES_PATH.name)
        return
    for field_name in PERSISTED_FIELDS:
        if field_name not in data:
            continue
        try:
            model = MODEL_LIST_FIELDS.get(field_name)
            if model is not None:
                setattr(s, field_name, [model(**item) for item in data[field_name]])
            else:
                setattr(s, field_name, data[field_name])
        except Exception:  # noqa: BLE001
            logger.warning("ignoring bad saved value for %s", field_name)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    _apply_overrides(s)
    # Seed only when nothing was configured or restored, so a user who deleted
    # every endpoint in the WebUI does not get them back on restart.
    if not s.tts_endpoints and not OVERRIDES_PATH.is_file():
        s.tts_endpoints = s.seed_endpoints()
    s.audio_dir.mkdir(parents=True, exist_ok=True)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    s.voices_dir.mkdir(parents=True, exist_ok=True)
    return s
