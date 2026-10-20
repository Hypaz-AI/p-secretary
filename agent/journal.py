"""Audit delle esecuzioni e journal idempotente degli effetti dell'agente.

* `agent_runs` / `agent_tool_calls`: cosa ha fatto l'agente (query eseguite,
  strumenti usati, esito). Solo metadati bounded; mai prompt né risposte.
* `agent_effects`: un effetto di scrittura è identificato da (turn_key, tool,
  hash degli argomenti). Se il job riparte dopo un crash o il modello ripete
  la stessa chiamata, l'effetto NON si ripete: si restituisce il risultato già
  committato. L'inserimento nel journal e l'effetto sono nella STESSA
  transazione: o ci sono entrambi o nessuno.
"""
import hashlib
import json

import db

MAX_ARGS = 2000
MAX_SUMMARY = 500


def _text(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + '…'


def start_run(kind, turn_key, job_id, owner_message, model):
    with db._conn() as conn:
        return conn.execute('''INSERT INTO agent_runs(kind,turn_key,job_id,owner_message,model)
            VALUES (%s,%s,%s,%s,%s) RETURNING id''', (kind, turn_key, job_id, owner_message, model)).fetchone()[0]


def finish_run(run_id, state, tool_calls, error=None):
    with db._conn() as conn:
        conn.execute('''UPDATE agent_runs SET state=%s,tool_calls=%s,error=%s,finished_at=now()
            WHERE id=%s''', (state, tool_calls, error[:200] if error else None, run_id))


def log_tool(run_id, seq, tool, args, ok, summary, duration_ms):
    try:
        with db._conn() as conn:
            conn.execute('''INSERT INTO agent_tool_calls(run_id,seq,tool,args,ok,summary,duration_ms)
                VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                (run_id, seq, tool, _text(args, MAX_ARGS), ok, _text(summary, MAX_SUMMARY), duration_ms))
    except Exception:
        pass  # L'audit non deve mai far fallire il turno.


def effect_key(tool, args):
    raw = json.dumps([tool, args], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def once(conn, turn_key, tool, args, apply):
    """Esegue `apply(conn)` al massimo una volta per (turn_key, tool, args).

    Ritorna (risultato, replayed). `apply` è eseguito nella stessa transazione
    della riga di journal: un'eccezione annulla entrambe.
    """
    key = effect_key(tool, args)
    with conn.transaction():
        claimed = conn.execute('''INSERT INTO agent_effects(turn_key,effect_key,tool)
            VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1''', (turn_key, key, tool)).fetchone()
        if not claimed:
            prior = conn.execute('SELECT result FROM agent_effects WHERE turn_key=%s AND effect_key=%s',
                                 (turn_key, key)).fetchone()
            return (prior[0] if prior and prior[0] is not None else {}), True
        result = apply(conn)
        conn.execute('UPDATE agent_effects SET result=%s WHERE turn_key=%s AND effect_key=%s',
                     (json.dumps(result, ensure_ascii=False, default=str), turn_key, key))
        return result, False


def executed(turn_key):
    """Effetti già committati per questo turno, in ordine (per i retry)."""
    with db._conn() as conn:
        return [dict(tool=tool, result=result) for tool, result in conn.execute(
            'SELECT tool,result FROM agent_effects WHERE turn_key=%s ORDER BY created_at,effect_key',
            (turn_key,)).fetchall()]
