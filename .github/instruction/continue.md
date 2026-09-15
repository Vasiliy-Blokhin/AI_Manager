---
description: Rules for generating patches for VS Code Continue agent
globs: **/*.py
alwaysApply: true
---

# Правила генерации кода для AI-агентов (VS Code Continue, Cline)

## Формат правок в файлах (CRITICAL)
При внесении изменений в существующий код ОБЯЗАТЕЛЬНО используйте блочный формат Markdown diff. 
Никогда не выдавайте просто обновленное содержимое всего файла.

Правильный шаблон:
```patch
--- a/path/to/file.py
+++ b/path/to/file.py
@@ -10,7 +10,7 @@ class MyClass:
-    def old_method(self):
-        pass
+    async def new_method(self):
+        """Новая реализация с поддержкой await."""
+        await some_async_call()