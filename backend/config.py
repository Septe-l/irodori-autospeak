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

# Starting points, meant to be replaced in the WebUI. Deliberately unnamed —
# give the character a name and the model will start saying it out loud.
DEFAULT_SYSTEM_PROMPT = f"""\
あなたは配信者のキャラクターです。与えられた話題について、調べた情報をもとに語ります。

- 一人称は「私」。
- 口調はタメ語。文末は「〜だよ」「〜だね」「〜かな」「〜なんだ」のような形。
- 性格は明るく元気。テンションは高め。

{SHARED_RULES}\
- 相手の返事を待つ問いかけで終わらない。一人で話し続ける前提で書く。
"""

PARTNER_SYSTEM_PROMPT = f"""\
あなたは配信者のキャラクターです。与えられた話題について、調べた情報をもとに語ります。

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
    kind: str = "reference"
    """"reference" clones a wav, "caption" takes a text description, "both" is a
    v4 checkpoint that does either."""
    enabled: bool = True

    def normalized(self) -> TTSEndpoint:
        url = self.url.strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        kind = self.kind if self.kind in ("reference", "caption", "both") else "reference"
        return TTSEndpoint(url=url, kind=kind, enabled=self.enabled)

    def serves(self, mode: str) -> bool:
        return self.kind == "both" or self.kind == mode


# Checkpoints the WebUI offers by name. Any other Hugging Face repo — including
# `repo/subfolder` for the quantized variants — can be typed in directly.
TTS_CHECKPOINTS: dict[str, str] = {
    "Aratako/Irodori-TTS-v4-Small": "v4 Small（推奨・参照音声とテキスト指定の両対応）",
    "Aratako/Irodori-TTS-v4-Small-Quantized/int8-weight-only": "v4 Small int8（省メモリ）",
    "Aratako/Irodori-TTS-v4-Small-Quantized/int4-weight-only": "v4 Small int4（最も省メモリ）",
    "Aratako/Irodori-TTS-500M-v3": "v3 500M（参照音声のみ）",
    "Aratako/Irodori-TTS-600M-v3-VoiceDesign": "v3 600M VoiceDesign（テキスト指定のみ）",
}


def kind_from_checkpoint(checkpoint: str) -> str:
    """Which voice styles a checkpoint can produce.

    v4 handles reference cloning and text-described voices in one model, so a
    single server covers both; the v3 line needed one checkpoint per style.
    """
    name = (checkpoint or "").lower()
    if "v4" in name:
        return "both"
    return "caption" if "voicedesign" in name else "reference"


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
    search_fetch_pages: int = 2
    """How many of those hits to download and extract article text from.
    Every page lands in the prompt, and prompt length is most of the LLM's GPU
    work — which the TTS is competing for when both run on one machine."""
    search_page_chars: int = 1200
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

    llm_reasoning_effort: str = "low"
    """For reasoning models (gpt-oss and the like). Their thinking is spent out of
    the same token budget as the answer, and at 'high' a turn can burn the whole
    budget and produce no speech at all. Non-reasoning models ignore the field.
    Set to "" to omit it entirely."""

    # --- Irodori-TTS ---
    # One machine runs one TTS server. Two checkpoints on the same Mac fight for
    # memory and GPU (measured: ~17% slower even when only one is synthesizing),
    # so a second checkpoint belongs on a second machine.
    tts_port: int = 8088
    """Port start_tts.sh launches the local TTS server on."""
    tts_checkpoint: str = "Aratako/Irodori-TTS-v4-Small"
    """Which checkpoint start_tts.sh loads. A Hugging Face repo, optionally with
    a subfolder for the quantized variants. v4 serves both voice styles; the v3
    line needs one server per style."""

    tts_ref_urls: str = ""
    """Seeds the endpoint list on first run: extra reference-checkpoint servers
    on other machines, comma-separated. The local server is added automatically.
    After first run the list is edited in the WebUI."""
    tts_caption_urls: str = ""
    """Same, for VoiceDesign servers on other machines."""

    tts_endpoints: list[TTSEndpoint] = Field(default_factory=list)
    """The live list. Empty means "derive it from the *_urls/*_port settings"."""
    tts_timeout: float = 180.0
    tts_num_steps: int = 20
    tts_speed: float = 1.0

    speaker_mode: str = "solo"
    """'solo' — speakers[0] only. 'duo' — the two speakers alternate turns."""

    speakers: list[Speaker] = Field(
        default_factory=lambda: [
            Speaker(
                name="話者A",
                persona=DEFAULT_SYSTEM_PROMPT,
                voice_mode="reference",
                voice_id="none",
                voice_caption=DEFAULT_VOICE_CAPTION,
            ),
            Speaker(
                name="話者B",
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
        # Two speakers are fine on a single TTS server as long as they both use
        # the kind it serves — two cloned voices, or two described ones. Only a
        # *mix* of the two kinds needs a second server.
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
        kinds: set[str] = set()
        for e in self.live_endpoints():
            kinds.update(("reference", "caption") if e.kind == "both" else (e.kind,))
        return sorted(kinds)

    def endpoints_for_mode(self, mode: str) -> list[str]:
        return [e.url for e in self.live_endpoints() if e.serves(mode)]

    def ref_endpoints(self) -> list[str]:
        return self.endpoints_for_mode("reference")

    def caption_endpoints(self) -> list[str]:
        return self.endpoints_for_mode("caption")

    def seed_endpoints(self) -> list[TTSEndpoint]:
        """Initial list: the local server, plus any remote ones from .env."""
        local = TTSEndpoint(url=f"http://127.0.0.1:{self.tts_port}", kind=self.local_kind())
        out = [local]
        for url in _split_urls(self.tts_ref_urls):
            out.append(TTSEndpoint(url=url, kind="reference"))
        for url in _split_urls(self.tts_caption_urls):
            out.append(TTSEndpoint(url=url, kind="caption"))
        seen: set[str] = set()
        return [e for e in out if not (e.url in seen or seen.add(e.url))]

    def local_kind(self) -> str:
        return kind_from_checkpoint(self.tts_checkpoint)

    # --- voice display names ---

    voice_labels: dict[str, str] = Field(default_factory=dict)
    """voice_id -> what to show in the UI.

    The TTS server only accepts ASCII ids, so a Japanese name cannot be the id
    itself; it is kept here and displayed in its place."""

    def label_for(self, voice_id: str) -> str:
        return self.voice_labels.get(voice_id) or voice_id

    # --- long-term memory (memory-mcp over Streamable HTTP) ---
    mcp_enabled: bool = False
    mcp_url: str = "http://127.0.0.1:8080/mcp"
    mcp_limit: int = 6
    """How many memories to pull into a turn's prompt."""
    mcp_timeout: float = 20.0

    # --- reference audio ---
    ref_target_seconds: float = 30.0
    """Uploads longer than this are trimmed. v4 takes up to 120s of reference and
    its model card asks for 30s or more; v3 preferred 10-15s."""

    # --- pacing ---
    prefetch_turns: int = 8
    """Stop generating new turns once this many un-played turns are buffered."""
    prefetch_seconds: float = 240.0
    """...or once this much un-played audio is buffered, whichever comes first.

    Depth matters more than it looks when the LLM and TTS share one GPU: until
    the buffer fills, the LLM never pauses, so it competes for the GPU without
    let-up and synthesis runs ~2.6x slower. Once the buffer does fill the LLM
    idles, the TTS gets the GPU to itself, and the system settles into a stable
    loop. A shallow buffer never reaches that point."""
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
    "llm_url",
    "llm_model",
    "llm_reasoning_effort",
    "llm_temperature",
    "speaker_mode",
    "speakers",
    "tts_endpoints",
    "tts_checkpoint",
    "tts_num_steps",
    "tts_speed",
    "prefetch_turns",
    "prefetch_seconds",
    "search_enabled",
    "searxng_url",
    "search_fetch_pages",
    "search_page_chars",
    "mcp_enabled",
    "mcp_url",
    "ref_target_seconds",
    "voice_labels",
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
