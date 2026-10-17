"""
Исполнители задач очереди: как выполнить чат / картинку / речь на уже запущенной модели.

Используются и синхронными эндпоинтами (/api/chat, /api/image, /api/tts, /v1/chat/completions),
и async-задачами (POST /api/jobs). Чат идёт асинхронным httpx к llama-server — его можно
прервать по таймауту/отмене (закрытие соединения останавливает генерацию).
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Optional

import httpx
from starlette.responses import StreamingResponse

from .backends.base import Backend, BackendError
from .jobs import JobCancelled, JobQueue, Ticket
from .schemas import ImageRequest, TTSRequest

_CLIENT: Optional[httpx.AsyncClient] = None


def client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        # Без таймаута чтения: длительность ограничивает очередь (AIM_JOB_RUN_TIMEOUT).
        _CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=30.0))
    return _CLIENT


def backend_of(queue: JobQueue, t: Ticket) -> Backend:
    return queue.manager._backend_for(queue.manager.registry.get(t.model))


def sanitize_messages(messages: list) -> list:
    """Убираем сообщения с пустым контентом — лишние токены префилла."""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("tool_calls") or m.get("role") == "tool":
            out.append(m)
            continue
        content = m.get("content")
        if content is None or (isinstance(content, str) and not content.strip()):
            continue
        out.append(m)
    return out


def prepare_chat_payload(be: Backend, payload: dict) -> dict:
    """Дефолты модели из реестра (request_defaults), чистка сообщений, кэш промпта."""
    payload = dict(payload)
    for k, v in getattr(be, "request_defaults", {}).items():
        payload.setdefault(k, v)
    payload["messages"] = sanitize_messages(payload.get("messages") or [])
    payload.setdefault("cache_prompt", True)
    payload.pop("project", None)
    return payload


def chat_summary(payload: dict) -> str:
    """Начало последнего сообщения пользователя — для просмотра очереди/истории."""
    for m in reversed(payload.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):  # мультимодальный формат OpenAI
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return str(c or "")[:300]
    return ""


def usage_of(data: dict) -> Optional[dict]:
    u = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(u, dict):
        return None
    return {k: u.get(k, 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}


def _base_url(be: Backend) -> str:
    url = getattr(be, "openai_base_url", None)
    if not url:
        raise BackendError(f"Бэкенд {be.backend_name} не поддерживает OpenAI-интерфейс")
    return url


async def chat_completion(queue: JobQueue, t: Ticket, payload: dict) -> tuple[int, dict]:
    """Нестримовый /chat/completions к llama-server. Возвращает (HTTP-код, JSON)."""
    be = backend_of(queue, t)
    url = f"{_base_url(be)}/chat/completions"
    body = prepare_chat_payload(be, payload)
    body["stream"] = False

    async def _call() -> tuple[int, dict]:
        try:
            r = await client().post(url, json=body)
        except httpx.HTTPError as e:
            raise BackendError(f"Запрос к модели не удался: {e}")
        try:
            return r.status_code, r.json()
        except ValueError:
            return 502, {"error": {"code": "backend_error", "message": r.text[:500]}}

    return await queue.run(t, _call())


def sse_error(code: str, message: str) -> bytes:
    body = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False)
    return f"data: {body}\n\ndata: [DONE]\n\n".encode()


async def chat_stream(queue: JobQueue, t: Ticket, payload: dict) -> AsyncIterator[bytes]:
    """
    SSE-стрим из llama-server байт-в-байт. Чтение upstream — отдельная прерываемая
    работа (queue.run), поэтому таймаут/отмена через API обрывают генерацию.
    Отключение клиента закрывает генератор → upstream закрывается.
    Задача закрывается (release) здесь же, в finally.
    """
    be = backend_of(queue, t)
    url = f"{_base_url(be)}/chat/completions"
    body = prepare_chat_payload(be, payload)
    body["stream"] = True
    chunks: asyncio.Queue = asyncio.Queue(maxsize=256)
    status, error = "done", None

    async def _pump() -> None:
        async with client().stream("POST", url, json=body) as r:
            if r.status_code != 200:
                text = (await r.aread()).decode("utf-8", "replace")[:1000]
                raise BackendError(f"HTTP {r.status_code}: {text}")
            async for chunk in r.aiter_raw():
                await chunks.put(chunk)
        await chunks.put(None)

    pump = asyncio.ensure_future(queue.run(t, _pump()))
    try:
        while True:
            getter = asyncio.ensure_future(chunks.get())
            done, _ = await asyncio.wait({getter, pump}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                chunk = getter.result()
                if chunk is None:
                    break
                yield chunk
                continue
            getter.cancel()
            exc = pump.exception()
            if exc is None:  # pump завершился — дочитать остаток
                while not chunks.empty():
                    chunk = chunks.get_nowait()
                    if chunk is not None:
                        yield chunk
                break
            raise exc
    except asyncio.CancelledError:
        status, error = "cancelled", "Клиент отключился во время генерации"
        raise
    except GeneratorExit:
        status, error = "cancelled", "Клиент отключился во время генерации"
        raise
    except Exception as e:
        if isinstance(e, JobCancelled):
            status, error = e.status, str(e)
            yield sse_error(e.status, str(e))
        else:
            status, error = "failed", f"Стрим от модели оборвался: {e}"
            yield sse_error("backend_error", error)
    finally:
        if not pump.done():
            pump.cancel()
        queue.release(t, status, error)


class JobStreamingResponse(StreamingResponse):
    """
    StreamingResponse, гарантированно закрывающий задачу: если клиент отключился до
    начала стрима (генератор не запускался) или посреди отправки — генератор
    закрывается явно (его finally прерывает генерацию), а задача освобождается.
    """

    def __init__(self, queue: JobQueue, t: Ticket, payload: dict, headers: dict):
        super().__init__(chat_stream(queue, t, payload), media_type="text/event-stream",
                         headers={**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        self._queue, self._ticket = queue, t

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.body_iterator.aclose()  # type: ignore[union-attr]
            except Exception:
                pass
            self._queue.release(self._ticket, "cancelled", "Клиент отключился до окончания стрима")


# ---------- исполнители async-задач (POST /api/jobs) ----------

async def exec_chat(queue: JobQueue, t: Ticket, payload: dict) -> dict:
    code, data = await chat_completion(queue, t, payload)
    if code != 200:
        raise BackendError(f"llama-server вернул HTTP {code}: {json.dumps(data, ensure_ascii=False)[:500]}")
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    result = {"model": t.model, "content": message.get("content") or "",
              "finish_reason": choice.get("finish_reason"), "usage": usage_of(data)}
    if message.get("reasoning_content"):
        result["reasoning_content"] = message["reasoning_content"]
    if message.get("tool_calls"):
        result["tool_calls"] = message["tool_calls"]
    return result


async def exec_image(queue: JobQueue, t: Ticket, payload: dict) -> dict:
    req = ImageRequest(**{k: v for k, v in payload.items() if k in ImageRequest.model_fields})
    resp = await queue.run_blocking(t, backend_of(queue, t).generate_image, req, t.cancel_event)
    return resp.model_dump()


async def exec_tts(queue: JobQueue, t: Ticket, payload: dict) -> dict:
    req = TTSRequest(**{k: v for k, v in payload.items() if k in TTSRequest.model_fields})
    resp = await queue.run_blocking(t, backend_of(queue, t).synthesize, req)
    return resp.model_dump()


def register_all(queue: JobQueue) -> None:
    queue.register_executor("chat", exec_chat)
    queue.register_executor("image", exec_image)
    queue.register_executor("tts", exec_tts)
