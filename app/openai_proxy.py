# app/openai_proxy.py
import os
import time
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status, Request
from pydantic import BaseModel, Field
from .manager import ModelManager

router = APIRouter()

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    """
    Схема запроса, совместимая с OpenAI API v1.
    Используется текстовыми агентами (Continue, Roo Code, Cursor) для генерации кода.
    """
    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 2048
    stream: Optional[bool] = False

class ImageGenerationRequest(BaseModel):
    prompt: str
    steps: Optional[int] = 25
    width: Optional[int] = 512
    height: Optional[int] = 512

class FileEditOperation(BaseModel):
    """
    Строгая схема для редактирования файлов.
    Режим Agent в Continue требует явного указания action.
    ВАЖНО: Для обхода бага с пустыми строками эндпоинт возвращает полный контент файла.
    """
    file_path: str
    action: str = Field(pattern="^(replace|insert|delete)$")
    old_str: Optional[str] = None
    new_str: Optional[str] = None

class OpenAIProxyHandler:
    """
    Бизнес-логика прокси-сервера.
    Изолирует работу с менеджером моделей от сетевых эндпоинтов FastAPI.
    Каждый публичный метод здесь соответствует одному типу задачи агента.
    """
    def __init__(self, manager: ModelManager):
        self.manager = manager

    async def chat_completion(self, payload: ChatCompletionRequest) -> Dict[str, Any]:
        """Обработка текстовых запросов к кодерским моделям (Qwen Coder)."""
        response_data = await self.manager.generate_chat(
            model_name=payload.model,
            messages=[msg.dict() for msg in payload.messages],
            temperature=payload.temperature,
            max_tokens=payload.max_tokens
        )
        
        choice = response_data["choices"][0]
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": choice["message"]["content"]},
                "finish_reason": choice.get("finish_reason", "stop")
            }],
            "usage": response_data.get("usage", {})
        }

    async def apply_file_edit(self, edit: FileEditOperation) -> Dict[str, Any]:
        """
        Применение патча к файлу файловой системы.
        Возвращает объект с ключом 'full_content', содержащий весь текст файла целиком.
        Это гарантирует, что расширение Continue получит данные и корректно перезапишет файл,
        минуя свой баг с некорректной вставкой диффов (удаление + пустая строка).
        """
        try:
            if not os.path.exists(edit.file_path):
                return {"result": "error", "detail": f"File not found: {edit.file_path}"}

            with open(edit.file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            patched_content = content
            
            if edit.action == "replace":
                if edit.old_str is None:
                    return {"result": "error", "detail": "old_str is required for replace."}
                patched_content = content.replace(edit.old_str, edit.new_str or "")
            elif edit.action == "insert":
                if edit.new_str is None:
                    return {"result": "error", "detail": "new_str is required for insert."}
                if edit.old_str:
                    parts = content.split(edit.old_str)
                    patched_content = edit.old_str.join([parts[0], edit.new_str, parts[1]])
                else:
                    patched_content = content + "\n" + edit.new_str
            elif edit.action == "delete":
                if edit.old_str is None:
                    return {"result": "error", "detail": "old_str is required for delete."}
                patched_content = content.replace(edit.old_str, "")

            with open(edit.file_path, 'w', encoding='utf-8') as f:
                f.write(patched_content)

            return {
                "result": "success",
                "action": edit.action,
                "file_path": edit.file_path,
                "full_content": patched_content # Выдача полного кода — главное исправление ошибки
            }

        except Exception as e:
            return {"result": "error", "detail": str(e)}

handler_instance: Optional[OpenAIProxyHandler] = None

async def get_manager(request: Request) -> ModelManager:
    return request.app.state.manager

@router.post("/chat")
async def proxy_chat(body: ChatCompletionRequest, manager: ModelManager = Depends(get_manager)):
    global handler_instance
    if handler_instance is None:
        handler_instance = OpenAIProxyHandler(manager)
    return await handler_instance.chat_completion(body)

@router.post("/apply_edit")
async def apply_edit(body: FileEditOperation, manager: ModelManager = Depends(get_manager)):
    """
    Отдельный эндпоинт для применения изменений.
    Вызывайте его из режима Agent, если стандартный механизм Ctrl+I ломает форматирование.
    """
    global handler_instance
    if handler_instance is None:
        handler_instance = OpenAIProxyHandler(manager)
    return await handler_instance.apply_file_edit(body)

# Эндпоинты /image и /models/* реализуются аналогично внутри этого же класса Handler.