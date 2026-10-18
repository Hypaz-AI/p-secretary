#!/usr/bin/env python3
"""PI-SECRETARY — Segretario virtuale sempre attivo sul Raspberry.

Pipeline:
1) Riassunto giornaliero via Telegram (digest.py, all'ora DIGEST_TIME).
2) Ogni input viene analizzato dall'AI: avviso se importante, altrimenti
   registrato e (per spam/newsletter) segnato come letto in mailbox.
3) Ogni messaggio alimenta il contesto su Postgres: comunicazioni,
   appuntamenti, note e regole insegnate dall'utente.
4) Le fatture in arrivo (allegati email, file Telegram, media WhatsApp)
   vengono salvate in INVOICE_DIR/<entità>/<aaaa-mm>/. L'AI decide a quale
   delle due P.IVA (hypaz / alessio) è intestata la fattura.
5) L'ultimo giorno del mese invia via Telegram uno zip per entità con le
   fatture del mese appena concluso.
"""
import re
import json
import signal
import sys
import time
from datetime import datetime

import actions
import admin_config
import brain
import config
import db
import digest
import dashboard
import flows
import invoices
from connectors import email as email_conn
from connectors import gcal
from connectors import telegram
from connectors.whatsapp import WhatsAppBridge
from connectors.telegram_account import TelegramAccountWorker

running = True


def _handle_signal(signum, frame):
    global running
    running = False
    print("\n[main] arresto richiesto, chiudo con calma...")


def format_verdict(channel: str, sender: str, subject: str, verdict: dict) -> str:
    """Costruisce il messaggio di notifica per l'utente."""
    esc = telegram.escape_html
    priority_emoji = {"alta": "🔴", "media": "🟡", "bassa": "🟢"}.get(verdict["priority"], "⚪")
    lines = [
        f"{priority_emoji} <b>Nuova comunicazione</b> ({esc(channel)})",
        f"<b>Da:</b> {esc(sender)}",
        f"<b>Urgenza:</b> {'sì' if verdict.get('urgent') is True else 'no'}",
        f"<b>Gravità:</b> {esc(verdict.get('severity', 'bassa'))}",
        f"<b>Motivo:</b> {esc(verdict.get('notification_reason') or 'non specificato')}",
    ]
    if subject:
        lines.append(f"<b>Oggetto:</b> {esc(subject)}")
    lines.append(f"<b>Riassunto:</b> {esc(verdict['summary'])}")
    if verdict.get("needs_action") and verdict.get("action"):
        lines.append(f"<b>👉 Azione:</b> {esc(verdict['action'])}")
    if verdict.get("needs_reply") and verdict.get("reply_hint"):
        lines.append(f"<b>💬 Risposta suggerita:</b> {esc(verdict['reply_hint'])}")
    for appt in verdict.get("appointments", []):
        lines.append(f"📅 <b>Appuntamento:</b> {esc(appt.get('title', ''))} — {esc(appt.get('datetime', ''))}"
                     + (f" @ {esc(appt['location'])}" if appt.get("location") else ""))
    for result in verdict.get("appointment_results", []):
        if result.get("ok"):
            lines.append("📆 Inserito su Google Calendar con promemoria popup")
        else:
            status = "Salvato solo in agenda" if result.get("local_saved") else "Non salvato"
            lines.append(f"⚠️ {status}: {telegram.escape_html(result.get('error', 'errore'))}")
    return "\n".join(lines)


def _save_invoice_attachments(channel: str, sender: str, subject: str,
                              verdict: dict, attachments: list, source: str) -> None:
    """Salva gli allegati-fattura rilevati dall'AI (pipeline 4)."""
    info = verdict.get("invoice_info") or {}
    # l'AI può restituire null nei campi: normalizza a stringa
    entity = (info.get("entity") or "sconosciuto").lower()
    info_date = info.get("date") or ""
    info_desc = info.get("description") or ""
    info_num = info.get("document_number") or ""
    for att in attachments:
        data = att.get("data")
        path = att.get("path")
        try:
            if data is not None:
                invoices.save_invoice(
                    entity=entity, filename=att["filename"], data=data,
                    sender=sender, subject=subject, source=source,
                    date=info_date,
                    description=info_desc,
                    number=info_num,
                )
            elif path:
                invoices.save_invoice_from_file(
                    entity=entity, path=path,
                    sender=sender, subject=subject, source=source,
                    date=info_date,
                    description=info_desc,
                    number=info_num,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[main] salvataggio fattura fallito: {exc}")
            flows.mark('invoices', 'failed')


def _legacy_process_message(channel: str, sender: str, subject: str, body: str,
                    state: dict, attachments: list = None,
                    account: dict = None, uid: str = None,
                    msg_date: str = None, source_id=None, metadata=None,
                    message_id=None) -> dict:
    """Analizza una comunicazione e agisce di conseguenza (pipeline 2, 3, 4).

    msg_date: timestamp REALE della comunicazione (header Date dell'email,
    formato 'YYYY-MM-DD HH:MM:SS' locale) — se None usa il momento della lettura.
    """
    attachments = attachments or []
    mid = message_id or db.store_message(channel, sender, subject, body,
        ts=msg_date, source_id=source_id, metadata=metadata, attachments=attachments)
    if source_id and db.communication_seen(channel, source_id):
        return {}
    print(f"[main] analizzo {channel} da {sender}: {subject[:60]}")
    verdict = brain.analyze(subject, sender, body, channel, attachments)
    normalized = brain.normalize_verdict(verdict)
    if isinstance(verdict, dict):
        verdict.update(normalized)  # preserva il contratto: risultati aggiunti al verdetto originale
    else:
        verdict = normalized

    # pipeline 3: registra nel contesto (DB) con la data REALE della email
    # Emergenze anche con soglia mai; invio prima di Calendar/fatture (potenzialmente lenti).
    emergency = verdict["urgent"] or verdict["severity"] == "alta"
    should_notify = emergency or config.priority_at_least(
        verdict["priority"], config.NOTIFY_PRIORITY_THRESHOLD)
    if emergency:
        telegram.notify(format_verdict(channel, sender, subject, verdict))
    comm_id = db.log_communication(
        channel, sender, subject, body, verdict, notified=should_notify,
        ts=msg_date, source_id=source_id, metadata=metadata, message_id=mid)

    # appuntamenti rilevati dall'AI
    verdict["appointment_results"] = []
    for appt in (verdict.get("appointments") or []):
        try:
            appt = gcal.validate_event(appt)
            db.add_appointment(
                title=appt.get("title", "Appuntamento"),
                dt=appt["dt"],
                location=appt.get("location", ""),
                notes=appt.get("notes", ""),
                source=f"{channel}: {sender}",
            )
            print(f"[main] 📅 appuntamento salvato: {appt.get('title')} {appt.get('datetime')}")
            result = _sync_appointment_to_gcal(appt, source=f"{channel}: {sender}")
            verdict["appointment_results"].append(result)
        except Exception as exc:  # noqa: BLE001
            print(f"[main] appuntamento non valido: {exc}")
            verdict["appointment_results"].append({"ok": False, "error": str(exc)})

    # pipeline 4: fatture in arrivo -> salvataggio organizzato
    if verdict.get("is_invoice") and attachments and flows.enabled('invoices'):
        flows.mark('invoices', 'running')
        _save_invoice_attachments(
            channel, sender, subject, verdict, attachments,
            source=f"{channel}: {sender}")
        if flows.observed().get('invoices', {}).get('status') != 'failed':
            flows.mark('invoices', 'idle')

    # pipeline 2: spam/pubblicità a bassa priorità -> segna come letto
    if (verdict["category"] in ("spam", "newsletter")
            and not config.priority_at_least(verdict["priority"], "media")
            and account and uid):
        if email_conn.mark_as_read(account, uid):
            print(f"[main] 🗑️ spam/newsletter segnata come letta: {subject[:50]}")

    # notifica l'utente solo se importante abbastanza
    if should_notify and not emergency:
        telegram.notify(format_verdict(channel, sender, subject, verdict))

    return verdict


def process_message(*args, **kwargs):
    import pipeline
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


def _sync_appointment_to_gcal(appt: dict, source: str = "") -> dict:
    """Riflette su Google Calendar un appuntamento rilevato dall'AI.

    Best-effort: l'agenda DB resta la fonte primaria, un fallimento qui
    non interrompe mai l'elaborazione del messaggio.
    """
    if not gcal.is_active():
        return {"ok": False, "local_saved": True, "error": "Google Calendar non attivo; nessun popup garantito"}
    try:
        res = gcal.create_event(
            title=appt.get("title", "Appuntamento"),
            start=appt.get("dt", appt.get("datetime", "")),
            location=appt.get("location", ""),
            notes=appt.get("notes", ""),
            source=source,
            duration_min=appt.get("duration_min"),
            reminder_minutes=appt.get("reminder_minutes"),
        )
        if res.get("ok"):
            print(f"[main] 📆 evento Google Calendar creato: {appt.get('title')}")
        else:
            print(f"[main] evento Google Calendar non creato: {res.get('error')}")
        return dict(res, local_saved=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[main] errore Google Calendar: {exc}")
        return {"ok": False, "local_saved": True, "error": str(exc)}


def _handle_user_command(text: str) -> bool:
    """Comandi che l'utente manda al bot per insegnare cose al segretario.
    Ritorna True se il testo era un comando ed è stato gestito."""
    t = text.strip()
    low = t.lower()
    import pipeline
    if pipeline.owner_commands(text):
        return True

    if low in ("/start", "/stop"):
        return True

    if low.startswith("/appuntamenti"):
        appts, source_label = None, ""
        # agenda "vera": Google Calendar se collegato e attivo
        try:
            if gcal.is_active():
                appts = gcal.upcoming_events(days=7)
                source_label = "Google Calendar"
        except Exception as exc:  # noqa: BLE001
            print(f"[main] lettura Google Calendar fallita: {exc}")
        if appts is None:  # fallback: agenda interna del segretario
            appts = db.upcoming_appointments()
            source_label = "agenda interna"
        else:
            from pipeline import merge_agenda
            appts = merge_agenda(appts, db.upcoming_appointments())
        lines = [f"• {telegram.escape_html(a['dt'])} — {telegram.escape_html(a['title'])}"
                 + (f" [#{a['id']} {telegram.escape_html(a.get('sync_state','local'))}]" if a.get('id') else '')
                 + (f" @ {telegram.escape_html(a['location'])}" if a.get("location") else "")
                 for a in appts]
        reply = (f"📅 <b>Prossimi appuntamenti</b> ({telegram.escape_html(source_label)}):\n"
                 + ("\n".join(lines) if lines else "Nessun appuntamento in programma."))
        telegram.notify(reply)
        return True

    if low.startswith("/note"):
        notes = db.recent_notes()
        telegram.notify("📝 <b>Ultime note:</b>\n" + ("\n".join(f"• {telegram.escape_html(n)}" for n in notes)
                                                 or "(nessuna)"))
        return True

    if low.startswith("/regole"):
        rules = db.all_rules()
        telegram.notify("📋 <b>Regole imparate:</b>\n" + ("\n".join(
            f"• SE {telegram.escape_html(r['pattern'])} ALLORA {telegram.escape_html(r['instruction'])}" for r in rules)
            or "(nessuna: scrivimi 'quando &lt;condizione&gt; allora &lt;istruzione&gt;')"))
        return True

    if low.startswith("/fatture"):
        months = db.months_with_invoices()
        if not months:
            telegram.notify("📦 Nessuna fattura salvata finora.")
        else:
            lines = []
            for m in months:
                lines.append(f"• <b>{telegram.escape_html(m['entity'])}</b> — {m['month']}: "
                             f"{len(db.invoices_for_month(m['entity'], m['month']))} fatture")
            telegram.notify("📦 <b>Fatture in archivio:</b>\n" + "\n".join(lines))
        return True

    # 'quando ... allora ...' -> nuova regola (pipeline 3: apprendimento)
    m = re.match(r"quando\s+(.+?)\s+allora\s+(.+)", t, re.IGNORECASE | re.DOTALL)
    if m:
        pattern, instruction = m.group(1).strip(), m.group(2).strip()
        db.add_rule(pattern, instruction)
        telegram.notify(f"✅ Regola imparata: <b>SE</b> {telegram.escape_html(pattern)} "
                        f"<b>ALLORA</b> {telegram.escape_html(instruction)}")
        return True

    return False


def _is_from_user(channel: str, sender: str, from_me: bool = False) -> bool:
    """True se il messaggio è scritto DIRETTAMENTE dall'utente al segretario
    (via Telegram o WhatsApp), e non è una comunicazione in arrivo da altri."""
    if channel == "telegram":
        # garantito dal filtro chat_id in connectors/telegram.py:
        # i messaggi di chat non autorizzate non arrivano mai fin qui
        return True
    if channel == "whatsapp":
        # from_me=True: inviata da un dispositivo collegato a questo
        # account (cioè dall'utente stesso, anche da un altro suo numero
        # non può essere: è lo stesso account WhatsApp)
        return False
    return False


# memoria della conversazione con l'utente (per continuità nel dialogo)
_chat_history = []


def handle_user_chat(text: str, channel: str = "telegram", images=None) -> None:
    """Dialogo con l'utente: risposta AI con contesto dal DB + memoria.

    Se l'AI fissa un appuntamento lo segnala con un marker @@EVENTO@@
    (vedi brain.CHAT_PROMPT): qui viene rimosso dal testo, salvato in
    agenda e — se Google Calendar è collegato — creato come evento.
    """
    global _chat_history
    message_id = getattr(_owner_context, 'message_id', None)
    if message_id is not None:
        import owner_dialogue
        owner_dialogue.respond(text, message_id, images=images)
        return
    print(f"[main] 💬 dialogo {channel}: {text[:60]}")
    kwargs = {'images': images} if images else {}
    reply = brain.chat_with_user(text, history=_chat_history, **kwargs)
    reply, events = gcal.parse_markers(reply)
    if events:
        # The model's prose may say 'Fatto' before tools run. Only actual
        # action results can confirm an operation, including in history.
        reply = _create_chat_appointments(events)
    content = ([{'type': 'text', 'text': text or 'Guarda questa immagine.'}] + images
               if images else text)
    _chat_history.append({"role": "user", "content": content})
    _chat_history.append({"role": "assistant", "content": reply})
    _chat_history = _chat_history[-10:]  # tieni gli ultimi scambi
    if not telegram.notify(reply, html=False):
        raise RuntimeError('Owner reply not acknowledged')


def _create_chat_appointments(events: list) -> str:
    """Crea gli appuntamenti richiesti nel dialogo (agenda + Calendar).

    Ritorna le righe di conferma da mostrare all'utente.
    """
    confirmations = []
    for ev in events:
        if ev.get("error"):
            confirmations.append(f"⚠️ Non sono riuscito a fissare «{ev.get('title') or ev.get('dt') or '?'}»: "
                                 f"{ev['error']}")
            continue
        try:
            ev = gcal.validate_event(ev)
            import pipeline
            import jobs
            key = 'owner:'+str(getattr(_owner_context, 'message_id', 'manual'))+':'+jobs.event_id(str(sorted(ev.items())))
            with db._conn() as conn, conn.transaction():
                identity = pipeline.save_event(conn, ev, key, 'dialogo utente')
        except Exception as exc:  # noqa: BLE001
            confirmations.append(f"⚠️ Non sono riuscito a fissare «{ev['title']}»: {exc}")
            continue
        line = f"📅 #{identity} «{ev['title']}» salvato localmente per il {ev['dt']}"
        if ev.get("location"):
            line += f" @ {ev['location']}"
        line += ' — sincronizzazione Calendar in coda, non ancora confermata'
        confirmations.append(line)
    return "\n".join(confirmations)


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
    import jobs
    import policy
    from connectors import media_text

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
                attachments=m.get('attachments'), reconcile=True)
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


import threading
_owner_context = threading.local()


def _owner_job(payload):
    # Scope archived identity to this job, including exceptions and retries.
    try:
        import owner_dialogue
        owner_dialogue.authorize(payload['message_id'])
        _owner_context.message_id = payload['message_id']
        _run_owner_job(payload)
    finally:
        _owner_context.message_id = None


def _run_owner_job(payload):
    import owner_dialogue
    import pipeline
    from connectors import media_text
    mid = payload['message_id']
    # Retry payloads cannot replace the archived current request or hide media.
    row, attachments = pipeline.load_message(mid)
    text = row[3]
    with db._conn() as conn:
        saved = conn.execute('SELECT plan,response FROM owner_exchanges WHERE message_id=%s', (mid,)).fetchone()
        direct = owner_dialogue._current_text(conn, mid, text)
    if saved and (saved[0] is not None or saved[1] is not None):
        # No model, media conversion or new data grant is needed to replay a
        # committed receipt/persisted plan. respond still fences owner authority.
        owner_dialogue.respond(text, mid)
        return
    if not attachments and direct and owner_dialogue.command(text, mid):
        return
    owner_dialogue.authorize(mid, ai=True, media=bool(attachments))
    if attachments:
        images = media_text.image_parts(attachments)
        # Only the owner's caption and voice are privileged current text.
        # Image OCR remains visible through vision/archive, never owner evidence.
        text = media_text.message_text(row[3], [a for a in attachments
                              if media_text.kind(a) == 'audio'])
        # Media contents must never become privileged slash commands/rules.
        if images:
            handle_user_chat(text, images=images)
        else:
            handle_user_chat(text)
    else:
        handle_user_chat(text)


def _reconcile_job(payload):
    """Do not overtake already queued earlier analysis or replay superseded edits."""
    import jobs
    import task_reconciliation
    mid = payload['message_id']
    with db._conn() as conn:
        if payload.get('revision'):
            current = conn.execute('''SELECT 1 FROM telegram_messages WHERE id=%s
                AND (metadata->>'revision_date')::timestamptz=%s::timestamptz''',
                (mid, payload['revision'])).fetchone()
            if not current:
                return
        earlier = conn.execute('''SELECT 1 FROM work_jobs j
            JOIN communications c ON c.id=(j.payload->>'message_id')::bigint
            WHERE j.kind='analysis' AND j.status IN ('pending','running')
              AND c.id<>%s AND c.acquired_at <=
                  (SELECT acquired_at FROM communications WHERE id=%s)
            LIMIT 1''', (mid, mid)).fetchone()
        if earlier:
            raise jobs.Deferred('Earlier message analysis is unfinished')
    task_reconciliation.reconcile(payload)


def _analysis_job(payload):
    """Indirect owner content is analyzed as evidence, never as task authority."""
    import jobs
    import pipeline
    import policy
    mid = payload['message_id']
    with db._conn() as conn:
        meta = policy.metadata_for(conn, mid)
    native = meta.get('metadata') or {}
    indirect = any(native.get(k) not in (None, False, '', {}) for k in (
        'is_forwarded', 'forward_origin', 'forward_date', 'forward_from', 'forward_from_chat',
        'forward_sender_name', 'forwarded', 'quote', 'external_reply', 'via_bot',
        'sender_chat', 'is_automatic_forward'))
    if meta.get('channel') != 'telegram' or not indirect:
        return pipeline.analyze(payload)
    if not flows.enabled('telegram_bot'):
        raise jobs.Deferred('Source paused')
    row, attachments = pipeline.load_message(mid)
    def gate(conn):
        jobs.assert_current(conn)
        if not policy.can_operate(conn, mid, 'extract'):
            raise jobs.Deferred('Analysis provenance no longer eligible')
        for effect in ('ai_text', 'ai_media') if attachments else ('ai_text',):
            if not policy.can_operate(conn, mid, effect):
                raise jobs.Deferred('Analysis AI data permission required in dashboard')
    with db._conn() as conn:
        gate(conn)
        if conn.execute('SELECT 1 FROM message_analysis WHERE message_id=%s', (mid,)).fetchone():
            return
    channel, sender, subject, body, source_id = row
    if attachments:
        from connectors import media_text
        body = media_text.message_text(body, attachments)
    with db._conn() as conn:
        gate(conn)
    verdict = brain.normalize_verdict(brain.analyze(subject, sender, body, channel, attachments))
    if verdict['category'] == 'errore':
        raise RuntimeError('Analysis unavailable')
    # Retain observations, not suggested commands/events from forwarded text.
    verdict.update(needs_action=False, action='', appointments=[])
    with db._conn() as conn, conn.transaction():
        gate(conn)
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (mid,))
        if conn.execute('SELECT 1 FROM message_analysis WHERE message_id=%s', (mid,)).fetchone():
            return
        db.message_store.analyze(conn, mid, channel, verdict, False, source_id)
        import secretary_runtime
        secretary_runtime.observe(conn, mid)
        should_notify = (verdict['urgent'] or verdict['severity'] == 'alta'
                        or config.priority_at_least(verdict['priority'], config.NOTIFY_PRIORITY_THRESHOLD))
        if should_notify and policy.can_operate(conn, mid, 'notify'):
            jobs.enqueue(conn, 'action', f'notify:{mid}', dict(type='notify', message_id=mid,
                text=format_verdict(channel, sender, subject, verdict)))
        import capabilities
        if verdict['is_invoice'] and attachments and capabilities.enabled('invoices', conn=conn):
            jobs.enqueue(conn, 'action', f'invoice:{mid}', dict(type='invoice', message_id=mid, verdict=verdict))
        jobs.assert_current(conn)


def _schedule_secretary_reviews():
    import secretary
    import secretary_runtime
    with db._conn() as conn, conn.transaction():
        secretary.due_reviews(conn)
        secretary_runtime.backlog(conn)


def _secretary_review_job(payload):
    """Use the version/source/consent-fenced, proposal-only runtime coordinator."""
    import secretary_runtime
    return secretary_runtime.review(payload)


def main():
    # Importing format_verdict from a worker must not register process signals.
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    print("=" * 60)
    print("  PI-SECRETARY — segretario virtuale")
    print(f"  Modello AI : {config.OPENROUTER_MODEL}")
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

    # pipeline 3: il contesto vive su Postgres
    try:
        db.init()
    except Exception as exc:  # noqa: BLE001
        print(f"\n[main] ERRORE: database non raggiungibile ({exc})")
        sys.exit(1)

    state = actions.load_state()
    import jobs
    import knowledge
    import research
    import secretary
    import pipeline
    import execution
    from connectors import media_archive
    state['telegram_offset'] = jobs.cursor('telegram_bot_offset', state.get('telegram_offset', 0))
    flows.init()
    web_server = dashboard.start()
    wa = WhatsAppBridge()
    personal = TelegramAccountWorker()
    import platform_health
    platform_health.register_workers(wa, personal)

    workers = [jobs.Worker('analysis', _analysis_job).start(),
               jobs.Worker('media', media_archive.process).start(),
               jobs.Worker('reconcile', _reconcile_job).start(),
               jobs.Worker('action', pipeline.action).start(),
               jobs.Worker('owner', _owner_job).start(),
               jobs.Worker('knowledge', knowledge.index_message).start(),
               jobs.Worker('secretary_review', _secretary_review_job).start(),
               jobs.Worker(execution.KIND, execution.execute_approved).start(),
               jobs.Worker(research.KIND, research.execute).start()]
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
                    launch('secretary_reviews', _schedule_secretary_reviews)
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
        actions.save_state(state)
    print("[main] arrivederci!")


if __name__ == "__main__":
    main()
