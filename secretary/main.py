"""PI-SECRETARY — segretario virtuale sempre attivo sul Raspberry.

Il lavoro intelligente lo fa UN agente (Copilot SDK, LLM via OpenRouter) con
strumenti tipizzati; qui restano solo il cablaggio e la raccolta dei dati:

1) Connettori (email, Telegram, WhatsApp) archiviano i messaggi nativi su
   Postgres e accodano un job durevole per ciascuno.
2) Worker `owner`: richieste dirette del proprietario -> comandi slash
   deterministici oppure agente in modalità dialogo (query DB libere in sola
   lettura, file in /data, posta live, ricerca web, scritture tipizzate).
3) Worker `analysis`: posta in arrivo -> agente in modalità triage
   (notifica solo ciò che è davvero prioritario).
4) Worker `digest`: riassunto serale scritto dall'agente e sigillato dal server.
5) Worker `action`: unico punto che compie effetti esterni (Telegram, Calendar,
   email approvate, fatture), sempre con ricevute e policy.
"""
import json
import signal
import sys
import threading
import time

from secretary.core import actions
from secretary.dashboard import admin_config
from secretary.core import config, db
from secretary.workflows import digest
from secretary.dashboard import dashboard
from secretary.core import flows
from secretary.connectors import email as email_conn, gcal, telegram
from secretary.connectors.whatsapp import WhatsAppBridge
from secretary.connectors.telegram_account import TelegramAccountWorker

running = True


def _handle_signal(signum, frame):
    global running
    running = False
    print("\n[main] arresto richiesto, chiudo con calma...")


def process_message(*args, **kwargs):
    from secretary.workflows import pipeline
    return pipeline.ingest(*args, **kwargs)


def check_emails(state: dict):
    """Legge SOLO le email nuove: gli UID già letti vivono nel DB (tabella
    email_seen), così il backlog non si rianalizza mai due volte."""
    for account in config.EMAIL_ACCOUNTS:
        if not running or not flows.enabled('email'):
            break  # arresto richiesto: esci subito, il resto lo vedrà al prossimo avvio
        key = account["address"].lower()
        seen = db.email_seen_uids(key)
        for msg in email_conn.fetch_new_messages(account, seen):
            if not running or not flows.enabled('email'):
                break  # non registrare: verrà rianalizzato al prossimo avvio
            process_message(
                channel=f"email ({account['name']})",
                sender=msg["from"],
                subject=msg["subject"],
                body=msg["body"],
                state=state,
                attachments=msg.get("attachments", []),
                account=account,
                uid=msg["uid"],
                msg_date=email_conn.parse_email_datetime(msg.get("date", "")),
                source_id=msg.get('source_id'), metadata=msg.get('metadata'),
            )
            # registra l'UID come letto nel DB (idempotente)
            db.mark_email_seen(key, msg.get('source_id') or msg['uid'])
        # UID identities are the authoritative durable email cursor. The
        # recovery scan itself intentionally overlaps the configured window.
    # pulizia periodica della tabella di tracciamento
    db.prune_email_seen()


def _wa_sender_label(m: dict) -> str:
    """Etichetta mittente WhatsApp leggibile dal modello e dall'utente.

    - chat privata in entrata: 'Nome (numero)'
    - gruppo in entrata: 'NomeAutore in NomeGruppo' (chi ha scritto e dove)
    - messaggio in uscita: 'io → NomeChat'
    L'ID tecnico resta nei metadata per il DB.
    """
    chat_name = m.get('from_name') or m.get('from') or '?'
    author_name = m.get('author_name')
    is_group = '@g.us' in str(m.get('chat_id') or m.get('from') or '')
    if m.get('from_me'):
        return f"io → {chat_name}"
    if is_group:
        if author_name:
            return f"{author_name} in {chat_name}"
        return f"{chat_name} · {m.get('author') or m.get('from')}"
    identity = m.get('chat_id') or m.get('from')
    return chat_name if chat_name == identity else f"{chat_name} ({identity})"


def check_telegram(state: dict):
    flows.mark('telegram_bot', 'running' if flows.enabled('telegram_bot') else 'paused')
    offset = state.get("telegram_offset", 0)
    download_dir = config.DATA_DIR / "tg_files"
    msgs, fetched_offset = telegram.fetch_new_messages(offset, download_dir=download_dir)
    from secretary.core import jobs, policy
    from secretary.connectors import media_text

    def checkpoint(conn, value):
        # Same commit as the native archive AND its processing job(s).
        conn.execute('''INSERT INTO source_cursors(key,value) VALUES (%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()''',
            ('telegram_bot_offset', json.dumps(value)))

    for m in msgs:
        if not running:
            return
        update_id = m.get('update_id')
        if type(update_id) is not int or update_id < offset:
            raise ValueError('Missing or unordered native Telegram update identity')
        metadata = dict(m.get('metadata') or {})
        authorized_chat = (not m.get('unauthorized')
                and telegram.is_owner_chat(m.get('chat_id'), m.get('chat_type'))
                and telegram.is_owner_chat(metadata.get('chat_id'), metadata.get('chat_type')))
        if not authorized_chat:
            with db._conn() as conn, conn.transaction():
                checkpoint(conn, update_id + 1)
            state['telegram_offset'] = offset = update_id + 1
            continue
        attachments = m.get('attachments') or []
        text = m.get('text') or ''
        indirect_fields = ('forward_origin', 'forward_date', 'forward_from', 'forward_from_chat',
                           'forward_sender_name', 'is_automatic_forward', 'is_forwarded',
                           'forwarded', 'quote', 'external_reply', 'via_bot', 'sender_chat')
        for key in indirect_fields:
            if key in m:
                metadata[key] = m[key]
        indirect = any(metadata.get(k) not in (None, False, '', {}) for k in indirect_fields)
        metadata['is_forwarded'] = indirect
        audio = any(media_text.kind(a) == 'audio' for a in attachments)
        # Attest transport text/caption, never extracted document text or a placeholder.
        # New connectors supply this directly; this adapter supports older transports.
        if 'content_origin' not in metadata:
            synthetic = text.startswith(('(file:', '\n[Media non scaricato:', '\n[Documento non scaricato:'))
            metadata['content_origin'] = ('forwarded' if indirect else 'direct_text'
                if text.strip() and not synthetic else 'direct_audio' if audio else 'attachment')
        chat_name = metadata.get('chat_name') or 'bot'
        with db._conn() as conn, conn.transaction():
            mid = db.message_store.store(conn, 'telegram', m['from'], f'Telegram · {chat_name}', text,
                ts=m.get('msg_date'), source_id=m.get('source_id'), metadata=metadata,
                attachments=attachments)
            owner = policy.can_operate(conn, mid, 'owner_command', owner_required=True)
            route_dialogue = metadata.get('content_origin') in ('direct_text', 'direct_audio')
            route_dialogue = route_dialogue or any(media_text.kind(a) == 'image' for a in attachments)
            if owner and not indirect and route_dialogue:
                # Any direct caption (PDF/XML/video/etc.), not just voice/photo.
                jobs.enqueue(conn, 'owner', f'owner:{mid}',
                             dict(text=text, message_id=mid, media=bool(attachments)))
            else:
                # Forwarded/indirect owner messages are evidence, never slash/rule authority.
                jobs.enqueue(conn, 'analysis', f'analysis:{mid}', dict(message_id=mid))
            checkpoint(conn, update_id + 1)
        state['telegram_offset'] = offset = update_id + 1
    # Filtered/empty transport updates have no content to archive. Never consume
    # this high-water mark on shutdown or after a partial/failed batch.
    if running and fetched_offset > offset:
        with db._conn() as conn, conn.transaction():
            checkpoint(conn, fetched_offset)
        state['telegram_offset'] = fetched_offset
    if flows.observed().get('telegram_bot', {}).get('status') == 'running':
        flows.mark('telegram_bot', 'idle' if config.TELEGRAM_BOT_TOKEN else 'not_configured')


def check_whatsapp(state: dict, bridge: WhatsAppBridge):
    for m in bridge.fetch_new_messages():
        if not running or not flows.enabled('whatsapp'):
            break
        sender = _wa_sender_label(m)
        chat_name = m.get('from_name') or m.get('from') or '?'
        try:
            mid = db.store_message('whatsapp', sender, f'WhatsApp · {chat_name}', m.get('text', ''),
                ts=m.get('msg_date'), source_id=m.get('id'), metadata=m.get('metadata'),
                attachments=m.get('attachments'))
            if db.communication_seen('whatsapp', m['id']):
                bridge.ack(m['id'], m.get('version'))
                continue
            if m.get('from_me'):
                db.log_communication('whatsapp', sender, f'WhatsApp · {chat_name}', m.get('text', ''),
                    {'category': 'outgoing', 'summary': m.get('text', ''), 'priority': 'bassa'},
                    False, ts=m.get('msg_date'), source_id=m['id'], message_id=mid)
            else:
                process_message('whatsapp', sender, f'WhatsApp · {chat_name}', m.get('text', ''),
                    state, attachments=m.get('attachments', []), msg_date=m.get('msg_date'),
                    source_id=m['id'], metadata=m.get('metadata'), message_id=mid)
            bridge.ack(m['id'], m.get('version'))
        except Exception as exc:
            flows.mark('whatsapp', 'failed')
            print(f'[whatsapp] message deferred ({type(exc).__name__})')


def sync_workers(wa, personal):
    for flow, configured, worker in (
            ('whatsapp', config.WHATSAPP_ENABLED, wa),
            ('telegram_user', config.TELEGRAM_USER_ENABLED, personal)):
        if not configured or not flows.enabled(flow):
            worker.stop()
            flows.mark(flow, 'paused' if configured else 'not_configured')
        elif flow == 'whatsapp':
            if not worker.proc or worker.proc.poll() is not None:
                worker.start()
            status = 'connected' if worker.ready else 'connecting'
            if flows.observed().get(flow, {}).get('status') != status:
                flows.mark(flow, status)
        elif not worker.thread or not worker.thread.is_alive():
            worker.start()


# ------------------------------------------------------------------ worker
def _owner_job(payload):
    """Richiesta diretta del proprietario: comando slash deterministico, altrimenti l'agente.

    Nessuna classificazione di intento: i soli comandi riconosciuti sono i
    gesti espliciti (/conferma, /invia, ...), che richiedono testo nativo corrente.
    """
    from secretary.core import jobs
    from secretary.workflows import pipeline
    from secretary.agent import owner, turns
    mid = payload['message_id']
    try:
        owner.authorize(mid)
        row, attachments = pipeline.load_message(mid)
        text = row[3]
        with db._conn() as conn:
            direct = owner.current_text(conn, mid, text)
        if not attachments and direct and owner.command(text, mid):
            return
        turns.owner_turn(mid)
    except PermissionError as exc:
        # L'autorità nativa non tornerà ad esistere aspettando: fallimento visibile, niente retry.
        raise jobs.Permanent('Owner authority is no longer valid') from exc


def _analysis_job(payload):
    """Posta in arrivo: l'agente fa il triage (anche per altri messaggi in attesa)."""
    from secretary.agent import turns
    return turns.inbox_job(payload)


def main():
    # Importing worker handlers must not register process signals.
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    print("=" * 60)
    print("  PI-SECRETARY — segretario virtuale (agentico)")
    print(f"  Modello AI : {config.AGENT_MODEL} (OpenRouter, via Copilot SDK)")
    print(f"  Email      : {len(config.EMAIL_ACCOUNTS)} account")
    for acc in config.EMAIL_ACCOUNTS:
        print(f"               • {acc['name']} ({acc['provider']}) — {acc['address']}")
    print(f"  Telegram   : {'attivo' if config.TELEGRAM_BOT_TOKEN else 'NON configurato'}")
    print(f"  WhatsApp   : {'attivo' if config.WHATSAPP_ENABLED else 'disattivato'}")
    print(f"  Calendar   : {'Google Calendar' if gcal.readiness() == 'ready' else gcal.readiness()}")
    print(f"  Database   : {'Postgres OK' if db.available() else 'NON configurato'}")
    print(f"  Fatture    : {config.INVOICE_DIR}")
    print(f"  Digest     : ogni giorno alle {config.DIGEST_TIME}")
    print(f"  Intervallo : {config.POLL_INTERVAL}s")
    print("=" * 60)

    if not config.OPENROUTER_API_KEY:
        print("\n[main] ERRORE: OPENROUTER_API_KEY mancante nel .env")
        sys.exit(1)

    # il contesto vive su Postgres
    try:
        db.init()
    except Exception as exc:  # noqa: BLE001
        print(f"\n[main] ERRORE: database non raggiungibile ({exc})")
        sys.exit(1)
    try:
        from secretary.agent import data as agent_data
        if agent_data.readonly_role() is None:
            print(f"[main] ATTENZIONE: il ruolo '{config.AGENT_DB_ROLE}' non è disponibile e l'utente del database "
                  "non può crearlo: le query libere dell'agente sono protette dalla sola guardia SQL. "
                  f"Il DBA esegua: CREATE ROLE {config.AGENT_DB_ROLE} NOLOGIN; "
                  f"GRANT {config.AGENT_DB_ROLE} TO <utente applicativo>; (vedi deploy/sql/agent_ro.sql); "
                  "poi riavvii l'app.")
    except Exception as exc:  # noqa: BLE001
        print(f"[main] verifica del ruolo di sola lettura non riuscita ({type(exc).__name__})")

    state = actions.load_state()
    from secretary.core import jobs
    from secretary.archive import knowledge
    from secretary.workflows import pipeline
    from secretary.connectors import media_archive
    state['telegram_offset'] = jobs.cursor('telegram_bot_offset', state.get('telegram_offset', 0))
    flows.init()
    web_server = dashboard.start()
    wa = WhatsAppBridge()
    personal = TelegramAccountWorker()
    from secretary.core import platform_health
    platform_health.register_workers(wa, personal)

    workers = [jobs.Worker('analysis', _analysis_job).start(),
               jobs.Worker('media', media_archive.process).start(),
               jobs.Worker('action', pipeline.action).start(),
               jobs.Worker('owner', _owner_job).start(),
               jobs.Worker('digest', digest.run).start(),
               jobs.Worker('knowledge', knowledge.index_message).start()]
    periodic = {}
    def launch(name, callback):
        old = periodic.get(name)
        if old and old.is_alive():
            return
        def run():
            try:
                callback()
            except Exception as exc:
                print(f'[main] {name} failed ({type(exc).__name__})')
        periodic[name] = threading.Thread(target=run, name=name, daemon=True)
        periodic[name].start()

    last_cycle = 0
    last_user_check = 0
    try:
        while running and not admin_config.restart_requested():
            try:
                now = time.time()
                sync_workers(wa, personal)
                if now - last_user_check >= 15:
                    last_user_check = now
                    check_telegram(state)
                    if config.WHATSAPP_ENABLED and flows.enabled('whatsapp'):
                        launch('whatsapp', lambda: flows.run('whatsapp', lambda: check_whatsapp(state, wa)))
                    actions.save_state(state)
                if now - last_cycle >= config.POLL_INTERVAL:
                    last_cycle = now
                    if config.EMAIL_ACCOUNTS:
                        launch('email', lambda: flows.run('email', lambda: check_emails(state)))
                    launch('digest', lambda: digest.maybe_send_digest(dict(state)))
                    launch('schedule', pipeline.schedule)
                    flows.mark('gcal', gcal.readiness() if flows.enabled('gcal') else 'paused')
            except Exception as exc:
                print(f'[main] cycle failed ({type(exc).__name__})')
            time.sleep(1)
    finally:
        for worker in workers:
            worker.stop()
        for close in (wa.stop, personal.stop, web_server.close):
            try:
                close()
            except Exception as exc:
                print(f'[main] shutdown error ({type(exc).__name__})')
        deadline = time.monotonic()+35
        for thread in [w.thread for w in workers]+list(periodic.values()):
            thread.join(timeout=max(0, deadline-time.monotonic()))
        try:
            from secretary.agent import runtime as agent_runtime
            agent_runtime.stop()
        except Exception as exc:
            print(f'[main] agent shutdown error ({type(exc).__name__})')
        actions.save_state(state)
    print("[main] arrivederci!")


if __name__ == "__main__":
    main()
