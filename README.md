# football-bot

Автопостинг футбольных новостей из RSS championat.com в @zafootball_news.
Запускается GitHub Actions каждые 30 минут, до 5 постов за запуск.

## Установка
1. Заменить файлы в репозитории файлами из этого архива (включая папку `.github`).
2. Settings → Secrets and variables → Actions → New secret: `BOT_TOKEN` = токен бота.
3. Settings → Actions → General → Workflow permissions: **Read and write**.
4. Actions → Publish football news → Run workflow (первый ручной запуск).

## Темы
rpl, epl, laliga, seriea, bundesliga, ligue1, eurocups, national, transfers, stars.
Включить только часть: раскомментировать `TOPICS` в `.github/workflows/publish.yml`.
Ключевые слова и стоп-слова: словари `TOPICS` и `STOP_WORDS` в `publish_once.py`.
