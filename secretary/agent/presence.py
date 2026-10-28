"""Feedback al proprietario mentre l'agente lavora (best effort, nessun effetto durevole).

Il «sta scrivendo» di Telegram viene rinnovato finché il turno è in corso. Se il turno
dura più di STATUS_AFTER secondi compare un unico messaggio di stato silenzioso che
indica il passo corrente; viene aggiornato e cancellato a fine turno, prima della
risposta. Solo etichette fisse: nessun contenuto di messaggi, allegati o risultati.
"""
import logging
import threading
import time

from secretary.connectors import telegram

logger = logging.getLogger(__name__)

TICK = 4            # s: il «sta scrivendo» di Telegram dura circa 5 secondi
STATUS_AFTER = 8    # s: prima di questa soglia la risposta arriva senza messaggi di stato
STATUS_EVERY = 20   # s: aggiornamento periodico anche senza cambio di passo
MIN_EDIT = 3        # s: distanza minima tra due modifiche consecutive
IDLE = 'Mmm, sto trovando robe davvero interessanti, aspetta un attimo'
STEPS = {
    'db_schema': 'Mmm, sto sfogliando lo schema dei dati',
    'db_query': 'Sto scavando nell\'archivio, qualcosa salterà fuori',
    'read_message': 'Rileggo il messaggio con calma, c\'è di più di quanto sembri',
    'attachment_text': 'Apro l\'allegato, promette bene',
    'fs_list': 'Do un\'occhiata alle cartelle, ce n\'è di belle',
    'fs_find': 'Frugo tra i file',
    'fs_read': 'Leggo il file con attenzione',
    'fs_write': 'Salvo tutto nel workspace',
    'fs_mkdir': 'Preparo una cartella nuova di zecca',
    'mail_accounts': 'Passo in rassegna le caselle di posta',
    'mail_search': 'Setaccio la posta, qualcosa di buono viene fuori',
    'mail_read': 'Apro l\'email, è interessante',
    'web_search': 'Faccio un giro sul web, ci sono novità',
    'notion_ask': 'Chiedo a Notion, ha delle chicche',
    'task_change': 'Aggiorno le attività una alla volta',
    'tasks_close_bulk': 'Metto ordine tra le attività',
    'note_save': 'Annoto tutto con cura',
    'note_forget': 'Cancello la nota, come richiesto',
    'calendar_propose': 'Preparo la proposta per il calendario',
    'email_propose': 'Scrivo la bozza, ci siamo quasi',
}


class Presence:
    """Un thread per turno: il passo corrente lo imposta il toolkit tramite step()."""

    def __init__(self):
        self._started = time.monotonic()
        self._step = IDLE
        self._step_lock = threading.Lock()   # solo il passo corrente: mai bloccato da Telegram
        self._io_lock = threading.Lock()     # chiamate Telegram e messaggio di stato
        self._stop = threading.Event()
        self._closed = False
        self._message_id = None
        self._shown = None
        self._edited_at = 0.0
        self._thread = threading.Thread(target=self._run, name='owner-presence', daemon=True)

    def start(self):
        self._thread.start()
        return self

    def step(self, tool):
        with self._step_lock:
            self._step = STEPS.get(tool, IDLE)

    def close(self):
        """Ferma il thread e rimuove il messaggio di stato: nessuna chiamata dopo questo punto."""
        self._stop.set()
        with self._io_lock:
            self._closed = True
            if self._message_id is not None:
                try:
                    telegram.delete_message(self._message_id)
                except Exception:  # noqa: BLE001
                    logger.debug('presence: status message not deleted', exc_info=True)
                self._message_id = None

    def _run(self):
        while True:
            self._tick()
            if self._stop.wait(TICK):
                return

    def _tick(self):
        with self._io_lock:
            if self._closed:
                return
            try:
                telegram.chat_action('typing')
                self._update_status()
            except Exception:  # noqa: BLE001
                logger.debug('presence: Telegram unavailable', exc_info=True)

    def _update_status(self):
        now = time.monotonic()
        elapsed = now - self._started
        if elapsed < STATUS_AFTER:
            return
        with self._step_lock:
            step = self._step
        text = f'⏳ {step}… ({int(elapsed)}s)'
        if self._message_id is None:
            self._message_id = telegram.send_status(text)
            self._shown, self._edited_at = step, now
            return
        changed = step != self._shown and now - self._edited_at >= MIN_EDIT
        if changed or now - self._edited_at >= STATUS_EVERY:
            telegram.edit_status(self._message_id, text)
            self._shown, self._edited_at = step, now
