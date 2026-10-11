"""
TTS Service wrapper encapsulating CapCut Text-to-Speech client operations.
Provides clean async-friendly methods for the FastAPI business routers.
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Union

from app.services.capcut_tts_api.client import CapCutClient
from app.services.capcut_tts_api.exceptions import CapCutError, CapCutTaskError

logger = logging.getLogger(__name__)


class TTSService:
    """
    Main TTS business service layer.
    Manages CapCut client lifecycle and executes TTS generation and task polling.
    """

    def __init__(self):
        self._client: Optional[CapCutClient] = None

    @property
    def client(self) -> CapCutClient:
        if self._client is None:
            self._client = CapCutClient()
        return self._client

    def list_available_voices(self, lang: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all available voices from the voice catalog.
        """
        voices = self.client.list_voices(lang=lang)
        return [
            {
                "voice_type": v.voice_type,
                "display_name": v.display_name,
                "resource_id": v.resource_id,
                "lang": v.lang,
                "lan": v.lan,
            }
            for v in voices
        ]

    async def generate_speech_async(
        self,
        texts: Union[str, List[str]],
        voice: Optional[str] = "BV074_streaming",
        resource_id: Optional[str] = None,
        rate: str = "1.0",
        wait: bool = True,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """
        Generate speech asynchronously via threadpool so FastAPI event loop is never blocked.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.generate_speech_sync(
                texts=texts,
                voice=voice,
                resource_id=resource_id,
                rate=rate,
                wait=wait,
                timeout=timeout,
            ),
        )

    def generate_speech_sync(
        self,
        texts: Union[str, List[str]],
        voice: Optional[str] = "BV074_streaming",
        resource_id: Optional[str] = None,
        rate: str = "1.0",
        wait: bool = True,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """
        Synchronously call CapCut client to generate speech and parse the response.
        """
        try:
            res = self.client.generate_speech(
                texts=texts,
                voice=voice,
                resource_id=resource_id,
                rate=rate,
                wait=wait,
                timeout=timeout,
            )
        except CapCutError as exc:
            logger.error(f"CapCut TTS error: {exc}")
            raise

        tasks = (res.get("data") or {}).get("tasks") or []
        if not tasks:
            return {"raw": res}

        task = tasks[0]
        task_id = task.get("id")
        status = task.get("status")
        token = task.get("token")

        audio_urls = []
        payload_data = None
        raw_payload = task.get("payload")

        if raw_payload:
            try:
                payload_data = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                for item in payload_data.get("audio_subtitles", []):
                    speech_url = item.get("speech_url")
                    if speech_url:
                        audio_urls.append(speech_url)
            except Exception as e:
                logger.warning(f"Could not parse task payload JSON: {e}")

        return {
            "task_id": task_id,
            "status": status,
            "token": token,
            "audio_urls": audio_urls,
            "payload": payload_data,
            "raw": res,
        }

    async def query_task_async(self, task_id: str, token: str) -> Dict[str, Any]:
        """
        Query an existing TTS task asynchronously.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.client.query_tts_task(task_id=task_id, token=token),
        )


# Singleton instance
tts_service = TTSService()
