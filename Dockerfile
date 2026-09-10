FROM python:3.11-slim

WORKDIR /srv/ai-manager
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ollama ставится отдельно (или используется внешний AIM_OLLAMA_URL).
# Для бэкенда llama_server смонтируйте бинарник llama-server в /usr/local/bin.
COPY . .

ENV AIM_REGISTRY=/srv/ai-manager/config/models.registry.json
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
