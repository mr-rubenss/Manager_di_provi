import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import msvcrt
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
FILES = ('state.json', 'subscribers.json', 'schedule_snapshot.json')
KEYS = ('TELEGRAM_BOT_TOKEN', 'SCHEDULE_SPREADSHEET_ID', 'SCHEDULE_SHEET_NAME',
        'WORKDAY_START', 'WORKDAY_END', 'ADMIN_TELEGRAM_IDS')
sys.path.insert(0, str(ROOT / 'app'))


def config():
    for key in (*KEYS, 'DAILY_HOUR'):
        os.environ.pop(key, None)
    os.environ['BOT_TIMEZONE'] = 'Europe/Moscow'
    path = ROOT / 'config.json'
    if path.exists():
        settings = json.loads(path.read_text(encoding='utf-8-sig'))
        for key in KEYS:
            if settings.get(key):
                os.environ[key] = settings[key]
    snapshot = DATA / FILES[2]
    if snapshot.exists():
        source = json.loads(snapshot.read_text(encoding='utf-8')).get('source', '')
        spreadsheet, _, sheet = source.partition('/')
        if spreadsheet and sheet:
            os.environ.setdefault('SCHEDULE_SPREADSHEET_ID', spreadsheet)
            os.environ.setdefault('SCHEDULE_SHEET_NAME', sheet)


def validate(directory):
    import bot
    import changes
    state, subscribers, snapshot = [
        json.loads((directory / name).read_text(encoding='utf-8')) for name in FILES
    ]
    store = bot.Store(directory / FILES[0], directory / FILES[1])
    records = changes.load_snapshot(directory / FILES[2], source=snapshot.get('source'))
    if not isinstance(state['offset'], int) or state['offset'] < 0:
        raise ValueError('Invalid offset')
    if store.state != state or len(store.subscribers) != len(subscribers):
        raise ValueError('Invalid state or subscribers')
    if records is None or len(records) != len(snapshot['records']):
        raise ValueError('Invalid schedule snapshot')
    return store, records


def backup(repo, revision, destination):
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    commit = subprocess.check_output(['git', '-C', repo, 'rev-parse', revision], text=True).strip()
    with tempfile.TemporaryDirectory(dir=destination.parent) as folder:
        staging, restored = (Path(folder) / name for name in ('backup', 'restored'))
        staging.mkdir()
        hashes = {}
        for name in FILES:
            raw = subprocess.check_output(['git', '-C', repo, 'show', f'{commit}:{name}'])
            (staging / name).write_bytes(raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
        shutil.copytree(staging, restored)
        for name, expected in hashes.items():
            if hashlib.sha256((restored / name).read_bytes()).hexdigest() != expected:
                raise ValueError('Restored file hash mismatch')
        store, records = validate(restored)
        (staging / 'manifest.json').write_text(json.dumps({
            'commit': commit, 'sha256': hashes, 'subscribers': len(store.subscribers),
            'muted': len(store.muted), 'schedule_records': len(records),
        }, indent=2) + '\n', encoding='utf-8')
        staging.rename(destination)
    print(f'Backup restored: commit={commit[:12]}, subscribers={len(store.subscribers)}, '
          f'muted={len(store.muted)}, schedule_records={len(records)}')


def smoke():
    import bot

    class FakeTelegram(bot.DryRunAPI):
        def __init__(self, updates):
            super().__init__(updates)
            self.replies = []

        def send_message(self, chat_id, text, keyboard=None):
            self.replies.append(text)

        def send_photo(self, chat_id, png, filename='schedule.png', caption=None):
            self.replies.append(caption or '[photo]')

    with tempfile.TemporaryDirectory() as folder:
        restore = Path(folder)
        for name in FILES:
            shutil.copyfile(DATA / name, restore / name)
        store, records = validate(restore)
        offset = store.state['offset']
        chat_id = int(next(iter(store.subscribers), '1'))
        updates = [
            {'update_id': offset + i, 'message': {'date': int(time.time()), 'text': command,
             'chat': {'id': chat_id, 'type': 'private'}}}
            for i, command in enumerate(('/help', '/today'))
        ]
        api = FakeTelegram(updates)
        instance = bot.Bot(api, store, bot.NameBook.load(), schedule_loader=lambda: records,
                           snapshot_file=restore / FILES[2])
        if instance.poll() != 2 or len(api.replies) != 2 or api.replies[0] != bot.HELP_TEXT:
            raise RuntimeError('Smoke test command flow failed')
        if store.state['offset'] != offset + 2:
            raise RuntimeError('Smoke test did not persist the offset')
        logging.info('SMOKE_OK replies=2 subscribers=%d', len(store.subscribers))


def read_schedule():
    import schedule_parser
    start = time.perf_counter()
    records = schedule_parser.load_schedule()
    if not records:
        raise ValueError('Google schedule contained no records')
    logging.info('google_sheet duration_ms=%d records=%d', (time.perf_counter() - start) * 1000,
                 len(records))
    return records


def check_telegram(api):
    start = time.perf_counter()
    api.call('getMe')
    info = api.call('getWebhookInfo')
    logging.info('telegram_check duration_ms=%d webhook=%s pending=%d',
                 (time.perf_counter() - start) * 1000, bool(info['url']),
                 info.get('pending_update_count', 0))
    if info['url']:
        raise RuntimeError('Telegram webhook active; polling requires an agreed cutover')


def serve(api, running=lambda: (ROOT / 'cutover.enabled').exists()):
    import bot

    store, _ = validate(DATA)
    latest = None
    poll_failures = 0
    fetched_at = next_refresh = next_cleanup = 0.0

    def get_schedule():
        if latest is None or time.monotonic() - fetched_at > 300:
            return read_schedule()
        return latest

    instance = bot.Bot(api, store, bot.NameBook.load(), schedule_loader=get_schedule,
                       snapshot_file=DATA / FILES[2], fresh_schedule_loader=read_schedule)
    # Only this thread processes Telegram updates and writes state.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = None
        while running():
            now = time.monotonic()
            if future is not None and future.done():
                try:
                    latest = future.result()
                except Exception as exc:
                    instance.note_error("Не удалось загрузить таблицу")
                    logging.warning('google_sheet failed type=%s', type(exc).__name__)
                else:
                    fetched_at = now
                    instance._schedule = latest
                    instance.last_sheet = instance.now()
                    instance.check_changes()
                future = None
            if future is None and now >= next_refresh:
                future = executor.submit(read_schedule)
                next_refresh = now + 60
            if now >= next_cleanup:
                if instance.cleanup_awaiting():
                    store.save()
                instance.sync_commands()
                next_cleanup = now + 60
            local = bot.local_now()
            if (latest is not None and now - fetched_at <= 120 and local.hour >= instance.daily_hour
                    and store.state.get('last_daily') != (local.date() + timedelta(days=1)).isoformat()):
                instance._schedule = latest
                instance.send_daily()
            if not running():
                break
            start = time.perf_counter()
            try:
                updates = api.call('getUpdates', offset=store.state['offset'], timeout=10, limit=100,
                                   allowed_updates=['message', 'callback_query'])
            except bot.TelegramError as exc:
                poll_failures += 1
                if exc.code is not None or poll_failures >= 3:
                    raise
                logging.warning('telegram_poll temporary failure attempt=%d code=%s',
                                poll_failures, exc.code)
                time.sleep(3 * poll_failures)
                continue
            poll_failures = 0
            if not updates:
                continue
            logging.info('telegram_updates count=%d poll_duration_ms=%d', len(updates),
                         (time.perf_counter() - start) * 1000)
            for update in updates:
                started = time.perf_counter()
                message_date = (update.get('message') or {}).get('date')
                age = max(0, round(time.time() - message_date)) if message_date else None
                instance._schedule = latest if time.monotonic() - fetched_at <= 300 else None
                try:
                    instance.handle_update(update)
                except Exception as exc:
                    instance.note_error(f"Ошибка обработки обновления: {type(exc).__name__}")
                    logging.warning('update failed type=%s', type(exc).__name__)
                handled = time.perf_counter()
                store.state['offset'] = update['update_id'] + 1
                store.save()
                logging.info('update age_seconds=%s handling_ms=%d state_write_ms=%d', age,
                             (handled - started) * 1000, (time.perf_counter() - handled) * 1000)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['backup', 'smoke', 'network', 'check', 'profile', 'run'])
    parser.add_argument('paths', nargs='*')
    args = parser.parse_args()
    config()
    if args.mode == 'backup':
        repo, revision, destination = args.paths
        backup(repo, revision, Path(destination))
        return 0

    log_dir = ROOT / 'logs'
    log_dir.mkdir(exist_ok=True)
    handler = TimedRotatingFileHandler(log_dir / 'bot.log', when='midnight', utc=True,
                                       backupCount=14, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format='%(asctime)s %(levelname)s %(message)s')
    import bot
    import requests
    bot.log = logging.info

    class TimedTelegram(bot.TelegramAPI):
        def _post(self, method, **request):
            start = time.perf_counter()
            try:
                return super()._post(method, **request)
            finally:
                if method != 'getUpdates':
                    logging.info('telegram_method name=%s duration_ms=%d', method,
                                 (time.perf_counter() - start) * 1000)

    try:
        if args.mode == 'smoke':
            smoke()
        elif args.mode == 'network':
            for _ in range(3):
                start = time.perf_counter()
                try:
                    response = requests.get('https://api.telegram.org/', timeout=12)
                    logging.info('telegram_https status=%d duration_ms=%d', response.status_code,
                                 (time.perf_counter() - start) * 1000)
                except requests.RequestException as exc:
                    logging.warning('telegram_https failed type=%s', type(exc).__name__)
                try:
                    read_schedule()
                except Exception as exc:
                    logging.warning('google_sheet failed type=%s', type(exc).__name__)
        elif args.mode == 'check':
            check_telegram(TimedTelegram(os.environ.get('TELEGRAM_BOT_TOKEN', '')))
            read_schedule()
            logging.info('PREFLIGHT_OK')
        elif args.mode == 'profile':
            return bot.setup_profile(apply=True, api=TimedTelegram(os.environ.get('TELEGRAM_BOT_TOKEN', '')))
        elif args.mode == 'run':
            if not (ROOT / 'cutover.enabled').is_file():
                raise RuntimeError('Cutover has not been armed')
            with (DATA / 'instance.lock').open('a+b') as lock:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                api = TimedTelegram(os.environ.get('TELEGRAM_BOT_TOKEN', ''))
                check_telegram(api)
                logging.info('POLLING_STARTED')
                serve(api)
    except bot.TelegramError as exc:
        logging.error('Telegram failure code=%s', exc.code)
        return 1
    except Exception as exc:
        logging.error('%s failed type=%s', args.mode, type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
