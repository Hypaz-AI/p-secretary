"""Read-only personal Telegram archive. No bot routing, sends or read receipts."""
import asyncio
import json
from pathlib import Path
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import db
import flows


def history_window(now=None):
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    now = now.astimezone(ZoneInfo(config.TIMEZONE))
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=1), today + timedelta(days=1)


def session_path():
    path = Path(config.TELEGRAM_USER_SESSION).expanduser()
    if not path.is_absolute():
        path = config.BASE_DIR / path
    if path.suffix != '.session':
        path = Path(str(path) + '.session')
    return path


def secure_session(path):
    """Use a dedicated directory; protect SQLite and its auxiliary files."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    for artifact in path.parent.glob(path.name + '*'):
        if artifact.is_file():
            artifact.chmod(0o600)


def api_credentials():
    try:
        api_id = int(config.TELEGRAM_API_ID)
    except (TypeError, ValueError):
        raise ValueError('Set a positive TELEGRAM_API_ID and TELEGRAM_API_HASH') from None
    if api_id <= 0 or not config.TELEGRAM_API_HASH:
        raise ValueError('Set a positive TELEGRAM_API_ID and TELEGRAM_API_HASH')
    return api_id, config.TELEGRAM_API_HASH


def display_name(entity) -> str:
    """Nome visualizzabile di un'entità Telethon (utente, gruppo o canale):
    'Nome Cognome (@user)' per le persone, il titolo per gruppi/canali."""
    if not entity:
        return ''
    title = getattr(entity, 'title', None)
    if title:
        return str(title)
    parts = [p for p in (getattr(entity, 'first_name', None),
                         getattr(entity, 'last_name', None)) if p]
    name = ' '.join(parts)
    username = getattr(entity, 'username', None)
    if name and username:
        return f'{name} (@{username})'
    return name or (f'@{username}' if username else '')


def readiness():
    """Local readiness only: never open the session or make Telegram RPCs."""
    if not config.TELEGRAM_USER_ENABLED:
        return 'config_disabled'
    try:
        api_credentials()
    except ValueError:
        return 'credentials_missing'
    if not session_path().is_file():
        return 'session_missing'
    return 'ready'


class TelegramAccountWorker:
    def __init__(self):
        self.thread = None
        self.loop = None
        self.task = None
        self.client = None
        self.account = None
        self.stopping = threading.Event()
        self.recovery_not_before = 0.0

    def start(self):
        if not config.TELEGRAM_USER_ENABLED or (self.thread and self.thread.is_alive()):
            return
        status = readiness()
        if status != 'ready':
            flows.mark('telegram_user', status)
            return
        self.stopping.clear()
        self.thread = threading.Thread(target=self._run, name='telegram-account', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.loop and self.task and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.task.cancel)
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                print('[telegram-account] shutdown still pending')

    def _run(self):
        flows.mark('telegram_user', 'connecting')
        # SQLite creates journals with session permissions; do NOT change the
        # process-wide umask in this background thread.
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(self._serve())
        try:
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            flows.mark('telegram_user', 'failed')
            # Exception text may contain credentials/phone numbers.
            print(f'[telegram-account] stopped ({type(exc).__name__}); check configuration/session and run setup_telegram_account.py manually')
        finally:
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            self.loop.close()

    async def _serve(self):
        try:
            from telethon import TelegramClient, events
        except ImportError:
            flows.mark('telegram_user', 'dependency_missing')
            print('[telegram-account] Telethon missing: install requirements.txt in the service virtualenv')
            return
        api_id, api_hash = api_credentials()
        path = session_path()
        if not path.is_file():
            flows.mark('telegram_user', 'session_missing')
            print('[telegram-account] session missing: run setup_telegram_account.py manually before enabling')
            return
        secure_session(path)
        if min(config.TELEGRAM_USER_DIALOG_LIMIT, config.TELEGRAM_USER_MESSAGE_LIMIT,
               config.TELEGRAM_USER_RECOVERY_INTERVAL) <= 0:
            raise ValueError('Recovery limits and interval must be positive')
        self.client = TelegramClient(str(path), api_id, api_hash,
                                     receive_updates=True, flood_sleep_threshold=0)
        try:
            await self.client.connect()  # never start(): no login prompts in systemd
            if not await self.client.is_user_authorized():
                flows.mark('telegram_user', 'unauthorized')
                print('[telegram-account] unauthorized: stop service and run setup_telegram_account.py manually')
                return
            me = await self.client.get_me()
            if me.bot:
                flows.mark('telegram_user', 'bot_session_refused')
                print('[telegram-account] bot session refused; authorize a personal account')
                return
            self.account = f'user:{me.id}'
            self.client.add_event_handler(self._on_message, events.NewMessage())
            self.client.add_event_handler(self._on_message, events.MessageEdited())
            print('[telegram-account] authorized; read-only archive active')
            flows.mark('telegram_user', 'connected')
            while not self.stopping.is_set():
                if not self.client.is_connected():
                    await self.client.connect()
                try:
                    await self.recover()
                except Exception as exc:
                    flows.mark('telegram_user', 'recovery_failed')
                    print(f'[telegram-account] bounded recovery failed ({type(exc).__name__}); retry next interval')
                await asyncio.sleep(max(config.TELEGRAM_USER_RECOVERY_INTERVAL,
                                        self.recovery_not_before - time.monotonic()))
        finally:
            await self.client.disconnect()
            secure_session(path)

    async def _on_message(self, event):
        try:
            await self.archive(event.message)
        except Exception as exc:
            flows.mark('telegram_user', 'archive_failed')
            print(f'[telegram-account] live archive failed ({type(exc).__name__}); bounded recovery will retry')

    async def archive(self, message, recovered=False):
        cutoff, end = history_window()
        if not message.date or not cutoff <= message.date < end or message.chat_id is None:
            return False
        # Service events (joins, pins, etc.) are not ordinary messages.
        if getattr(message, 'action', None) is not None:
            return False
        chat_id = str(message.chat_id)  # Telethon marked peer ID, including -100 channels
        source_id = json.dumps(['mtproto', self.account, chat_id, message.id], separators=(',', ':'))
        revision = message.edit_date or message.date
        # Nomi leggibili: prima la cache locale (nessuna RPC), poi la
        # risoluzione bounded di Telethon solo se l'entità manca del tutto.
        sender = getattr(message, 'sender', None)
        if sender is None:
            try:
                sender = await message.get_sender()
            except Exception:
                sender = None
        chat = getattr(message, 'chat', None)
        if chat is None:
            try:
                chat = await message.get_chat()
            except Exception:
                chat = None
        sender_name = display_name(sender)
        chat_name = display_name(chat)
        chat_type = ('private' if message.is_private else
                     'group' if message.is_group else 'channel')
        # Etichetta mittente che il modello e l'utente possono leggere:
        # non un ID numerico ma CHI ha scritto e DOVE.
        if message.out:
            sender_label = f'io → {chat_name or chat_id}'
        elif chat_type != 'private' and sender_name and chat_name:
            sender_label = f'{sender_name} in {chat_name}'
        elif sender_name:
            sender_label = sender_name
        elif chat_name:
            # canale o mittente non risolvibile: la chat È il mittente
            sender_label = chat_name
        else:
            sender_label = str(message.sender_id or chat_id)
        meta = {'account': self.account, 'chat_id': chat_id,
                'message_id': str(message.id), 'author': message.sender_id,
                'direction': 'out' if message.out else 'in',
                'revision_date': revision.isoformat(),
                'edit_date': message.edit_date.isoformat() if message.edit_date else None,
                'recovered': recovered, 'transport': 'mtproto',
                'has_media': bool(message.media),
                'chat_type': chat_type,
                'sender_name': sender_name, 'chat_name': chat_name}
        # Native storage only: no AI calls, commands, contact replies or media downloads.
        await asyncio.to_thread(db.store_message, 'telegram (account)',
                                sender_label, chat_name or '(Telegram personale)',
                                message.raw_text or ('[Telegram media]' if message.media else ''), ts=message.date.isoformat(),
                                source_id=source_id, metadata=meta, update_existing=True)
        return True

    async def recover(self):
        from telethon.errors import FloodWaitError

        if time.monotonic() < self.recovery_not_before:
            return
        try:
            await self._recover_pass()
        except FloodWaitError as exc:
            delay = max(0, int(exc.seconds))
            self.recovery_not_before = time.monotonic() + delay
            flows.mark('telegram_user', 'rate_limited')
            print(f'[telegram-account] recovery rate limited; deferred for {delay}s; live archive remains active')

    async def _recover_pass(self):
        from telethon.errors import FloodWaitError

        cutoff, end = history_window()
        dialogs = messages = stored = capped = 0
        async for dialog in self.client.iter_dialogs(limit=config.TELEGRAM_USER_DIALOG_LIMIT):
            if self.stopping.is_set():
                break
            dialogs += 1
            count = 0
            try:
                async for message in self.client.iter_messages(
                        dialog.entity, limit=config.TELEGRAM_USER_MESSAGE_LIMIT, offset_date=end):
                    if self.stopping.is_set():
                        break
                    count += 1
                    messages += 1
                    if message.date and message.date < cutoff:
                        break
                    stored += bool(await self.archive(message, recovered=True))
                capped += count == config.TELEGRAM_USER_MESSAGE_LIMIT
            except FloodWaitError:
                # Stop the entire pass, not just this dialog. No further RPCs
                # until the server-requested cooldown has elapsed.
                raise
            except Exception as exc:
                print(f'[telegram-account] dialog recovery failed ({type(exc).__name__}); continuing')
        print(f'[telegram-account] bounded yesterday/today recovery: dialogs={dialogs}/'
              f'{config.TELEGRAM_USER_DIALOG_LIMIT}, inspected={messages}, stored={stored}, '
              f'message-cap dialogs={capped}; caps may omit messages; no full history paging')