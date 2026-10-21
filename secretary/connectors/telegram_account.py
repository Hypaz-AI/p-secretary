"""Read-only personal Telegram archive. No bot routing, sends or read receipts."""
import asyncio
import hashlib
import io
import json
import math
from pathlib import Path
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from secretary.core import config, db, flows


def history_window(now=None):
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    now = now.astimezone(ZoneInfo(config.TIMEZONE))
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=max(1,min(90,int(config.get('RECOVERY_DAYS','14'))))), today + timedelta(days=1)


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


def excluded_chat(chat_id) -> bool:
    """Chat esclusa da TELEGRAM_EXCLUDED_CHATS (ID Telethon marcato, come chat_id dei messaggi)."""
    return chat_id is not None and str(chat_id) in config.TELEGRAM_EXCLUDED_CHATS


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
        from secretary.connectors.storage import FileLock
        try:
            with FileLock(str(session_path()) + '.owner.lock', blocking=False):
                self._run_locked()
        except OSError:
            flows.mark('telegram_user', 'session_busy')
            print('[telegram-account] session occupied or inaccessible')

    def _run_locked(self):
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
            print(f'[telegram-account] stopped ({type(exc).__name__}); check configuration/session and run python -m secretary.setup.telegram_account manually')
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
            print('[telegram-account] session missing: run python -m secretary.setup.telegram_account manually before enabling')
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
                print('[telegram-account] unauthorized: stop service and run python -m secretary.setup.telegram_account manually')
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

    async def media_attachments(self, message, source_id):
        """Bounded downloads through the already-owned MTProto client, no sends."""
        from secretary.connectors import media_text
        file = getattr(message, 'file', None)
        if not file:
            return []
        mime = getattr(file, 'mime_type', None) or ''
        if getattr(message, 'photo', None):
            mime = 'image/jpeg'
        if not media_text.kind({'content_type': mime}):
            return []
        size = getattr(file, 'size', 0) or 0
        if size > media_text.MAX_BYTES:
            raise ValueError('Media Telegram oltre il limite di 10 MiB')

        def bounded(received, total):
            if received > media_text.MAX_BYTES or (total and total > media_text.MAX_BYTES):
                raise ValueError('Media Telegram oltre il limite di 10 MiB')

        media = getattr(message, 'photo', None) or getattr(message, 'document', None)
        identity = f"{source_id}:{getattr(media, 'id', '')}"
        name = hashlib.sha256(identity.encode()).hexdigest()
        with io.BytesIO() as stream:
            await self.client.download_media(message, file=stream, progress_callback=bounded)
            data = stream.getvalue()
        if not data or len(data) > media_text.MAX_BYTES:
            raise ValueError('Media Telegram vuoto o oltre il limite di 10 MiB')
        return [{'filename': name, 'content_type': mime, 'data': data,
                 'duration': getattr(file, 'duration', None)}]

    async def archive(self, message, recovered=False, window=None, *, historical=False):
        # Blocco a monte: chat escluse prima di qualsiasi lettura del mittente/chat,
        # download di media o scrittura. Vale per live, recupero e import storico.
        if excluded_chat(message.chat_id):
            return False
        cutoff, end = history_window() if window is None else window
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
        attachments = await self.media_attachments(message, source_id)
        # Solo archivio: nessuna AI inline, comando o risposta. Gli import storici
        # sono marcati esplicitamente, indipendentemente da `recovered`.
        await asyncio.to_thread(db.store_message, 'telegram (account)',
                                sender_label, chat_name or '(Telegram personale)',
                                message.raw_text or ('[Telegram media]' if message.media else ''), ts=message.date.isoformat(),
                                source_id=source_id, metadata=meta, attachments=attachments,
                                update_existing=True,
                                historical=historical)
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
            if excluded_chat(getattr(dialog, 'id', None)):
                continue  # nessuna richiesta di messaggi per le chat escluse
            dialogs += 1
            count = 0
            from secretary.core import recovery
            key = 'mtproto:'+str(self.account)+':'+str(getattr(dialog, 'id', dialog.entity))
            dialog_cutoff = max(cutoff, await asyncio.to_thread(recovery.scan_start, key))
            try:
                async for message in self.client.iter_messages(
                        dialog.entity, limit=config.TELEGRAM_USER_MESSAGE_LIMIT, offset_date=end):
                    if self.stopping.is_set():
                        break
                    count += 1
                    messages += 1
                    if message.date and message.date < dialog_cutoff:
                        break
                    stored += bool(await self.archive(message, recovered=True))
                capped += count == config.TELEGRAM_USER_MESSAGE_LIMIT
                if count < config.TELEGRAM_USER_MESSAGE_LIMIT:
                    await asyncio.to_thread(recovery.completed, key)
            except FloodWaitError:
                # Stop the entire pass, not just this dialog. No further RPCs
                # until the server-requested cooldown has elapsed.
                raise
            except Exception as exc:
                print(f'[telegram-account] dialog recovery failed ({type(exc).__name__}); continuing')
        print(f'[telegram-account] bounded yesterday/today recovery: dialogs={dialogs}/'
              f'{config.TELEGRAM_USER_DIALOG_LIMIT}, inspected={messages}, stored={stored}, '
              f'message-cap dialogs={capped}; caps may omit messages; no full history paging')


async def import_history(start, end, max_flood_wait=300):
    """Archive all personal dialogs in aware [start, end); return stored count.

    Requires an existing authorized personal session. No login, live listeners,
    AI, recovery watermarks or configured recovery caps. Flood waits are retried
    within a cumulative seconds budget; other errors propagate. Replayed scans
    use native storage identity/upserts and count each archived message once.
    """
    from secretary.connectors.storage import FileLock

    if any(not isinstance(d, datetime) or d.utcoffset() is None for d in (start, end)):
        raise ValueError('History bounds must be timezone-aware datetimes')
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if start >= end:
        raise ValueError('History start must precede end')
    if (not isinstance(max_flood_wait, (int, float)) or
            not math.isfinite(max_flood_wait) or max_flood_wait < 0):
        raise ValueError('max_flood_wait must be finite non-negative seconds')
    path = session_path()
    if not path.is_file():
        raise FileNotFoundError('Existing personal Telegram session required')
    api_id, api_hash = api_credentials()
    with FileLock(str(path) + '.owner.lock', blocking=False):
        # Recheck under the same ownership lock as the live worker.
        if not path.is_file():
            raise FileNotFoundError('Existing personal Telegram session required')
        from telethon import TelegramClient
        secure_session(path)
        client = TelegramClient(str(path), api_id, api_hash,
                                receive_updates=False, flood_sleep_threshold=0)
        waited = 0

        async def retry(operation):
            nonlocal waited
            while True:
                try:
                    return await operation()
                except Exception as exc:
                    # Keep Telethon optional, including for offline client mocks.
                    try:
                        from telethon.errors import FloodWaitError
                    except ImportError:
                        raise exc from None
                    if not isinstance(exc, FloodWaitError):
                        raise
                    delay = max(1, math.ceil(exc.seconds))
                    if waited + delay > max_flood_wait:
                        raise RuntimeError(
                            f'Telegram history requires {delay}s cooldown; '
                            f'cumulative wait budget {max_flood_wait:g}s exceeded '
                            f'after {waited}s. Retry later or increase '
                            'max_flood_wait; already archived messages are safe '
                            'to reimport.') from None
                    print(f'[telegram-account] history rate limited; waiting {delay}s '
                          f'(cumulative {waited + delay}s/{max_flood_wait:g}s)')
                    waited += delay
                    # Hold ownership and make no further requests until cooldown.
                    await asyncio.sleep(delay)

        try:
            await retry(client.connect)
            if not await retry(client.is_user_authorized):
                raise PermissionError('Telegram session is not authorized')
            me = await retry(client.get_me)
            if me is None or me.bot:
                raise PermissionError('Personal Telegram session required')
            worker = TelegramAccountWorker()
            worker.client = client
            worker.account = f'user:{me.id}'
            archived = set()

            async def scan():
                # A failed async iterator cannot be resumed. Restart the full
                # scan, without advancing recovery watermarks or double counting.
                async for dialog in client.iter_dialogs(limit=None):
                    if excluded_chat(getattr(dialog, 'id', None)):
                        continue
                    async for message in client.iter_messages(dialog.entity, limit=None,
                                                               offset_date=end):
                        if message.date and message.date < start:
                            break
                        identity = (message.chat_id, message.id, message.edit_date or message.date)
                        if identity in archived:
                            continue
                        if await worker.archive(message, recovered=True, window=(start, end),
                                                historical=True):
                            archived.add(identity)

            await retry(scan)
            return len(archived)
        finally:
            try:
                await client.disconnect()
            finally:
                secure_session(path)