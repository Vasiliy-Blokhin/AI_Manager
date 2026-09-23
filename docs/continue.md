# Настройка Continue для AI Manager

## 1. Установка Continue в VS Code

Расширение: `Continue` (id: `Continue.continue`)

## 2. Настройка config.yaml

Файл: `%USERPROFILE%\.continue\config.yaml`

```yaml
name: AI Manager Local
version: 1.0.0
schema: v1

models:
  - name: Qwen2.5 Coder 14B (Local)
    provider: openai
    model: qwen25-coder-14b-unc
    apiBase: http://127.0.0.1:8000/v1
    apiKey: ваш_пароль_из_.env
    useLegacyCompletionsEndpoint: false
    # ВАЖНО: не используйте /v1/completions — только /v1/chat/completions

context:
  - provider: code
  - provider: docs

# Системный промпт для режима агента — задаёт правила применения правок
systemMessage: |
  You are an expert software engineer.
  When editing files, you MUST use the apply_diff tool with unified diff format.
  NEVER output the entire file content — only the diff.
  Always include enough context lines (3+) for unique matching.
  Use create_new_file tool only for new files.