"""Send a free Telegram alert when GitHub Actions fails."""
import os
import sys
import urllib.parse
import urllib.request

chat_id = os.getenv('ALERT_CHAT_ID')
token = os.getenv('BOT_TOKEN')
if not chat_id or not token:
    print('ALERT_CHAT_ID is not configured; skipping alert')
    raise SystemExit(0)

repo = os.getenv('GITHUB_REPOSITORY', 'football-bot')
run = os.getenv('GITHUB_SERVER_URL', 'https://github.com') + '/' + repo + '/actions/runs/' + os.getenv('GITHUB_RUN_ID', '')
text = '🚨 Футбольный бот не смог опубликовать новости.\nПроверь GitHub Actions: ' + run
url = f'https://api.telegram.org/bot{token}/sendMessage'
data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text, 'disable_notification': 'false'}).encode()
with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as response:
    print('Failure alert sent:', response.status)
