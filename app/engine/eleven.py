"""ElevenLabs text-to-speech over plain HTTPS (no SDK, no ffmpeg): raw PCM16 in, WAV out.

`synth` blocks (it runs on the shared synth executor, like every engine). Any failure raises
ElevenLabsError; app/engine/router.py turns that into a fallback to the local EMA engine.
"""
from __future__ import annotations

import io
import logging
import wave

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io"
PCM_RATES = (16000, 22050, 24000, 44100)  # pcm_* formats available without an enterprise plan; 44100 needs Pro
SPEED_MIN, SPEED_MAX = 0.7, 1.2  # ElevenLabs voice_settings.speed range
WARMUP_TEXT = "Merhaba."


class ElevenLabsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def permanent(self) -> bool:
        """Key / quota / voice problems do not fix themselves in seconds: the router backs off for longer."""
        return self.status in (400, 401, 402, 403, 404, 422)


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("status") or detail
        return str(detail)[:300]
    except ValueError:
        return (resp.text or resp.reason_phrase or "")[:300]


class ElevenLabsEngine:
    name = "elevenlabs"

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model_id: str = "eleven_turbo_v2_5",
        timeout_s: float = 8.0,
        stability: float = 0.5,
        similarity_boost: float = 0.75,
        style: float = 0.0,
        speed: float = 1.0,
        language: str = "tr",
        client: httpx.Client | None = None,
    ) -> None:
        self._key = api_key
        self.voice_id = voice_id
        self.model_id = model_id
        self._stability = stability
        self._similarity = similarity_boost
        self._style = style
        self.speed = min(max(float(speed), SPEED_MIN), SPEED_MAX)
        self._language = language
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_s, connect=3.0))

    def _body(self, text: str) -> dict:
        body: dict = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": {
                "stability": self._stability,
                "similarity_boost": self._similarity,
                "style": self._style,
                "use_speaker_boost": True,
                "speed": self.speed,
            },
        }
        # language_code is rejected by multilingual_v2 and older models; flash/turbo v2.5 accept it.
        if self._language and "v2_5" in self.model_id:
            body["language_code"] = self._language
        return body

    def synth(self, text: str, sample_rate: int = 24000) -> bytes:
        rate = sample_rate if sample_rate in PCM_RATES else 24000
        try:
            resp = self._client.post(
                f"{API_BASE}/v1/text-to-speech/{self.voice_id}",
                params={"output_format": f"pcm_{rate}"},
                headers={"xi-api-key": self._key, "Content-Type": "application/json", "Accept": "application/octet-stream"},
                json=self._body(text),
            )
        except httpx.HTTPError as exc:
            raise ElevenLabsError(f"{type(exc).__name__}: {exc}"[:300]) from None
        if resp.status_code != 200:
            raise ElevenLabsError(f"HTTP {resp.status_code}: {_error_text(resp)}", resp.status_code)
        pcm = resp.content
        if len(pcm) < 2:
            raise ElevenLabsError("empty audio")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm[: len(pcm) - (len(pcm) % 2)])
        return buf.getvalue()

    def warmup(self) -> None:  # not used (the local engine warms up); present for the Engine protocol
        return None

    def close(self) -> None:
        self._client.close()
