"""Dashboard con dati FITTIZI su un database usa-e-getta, per sviluppare e controllare la grafica.

Uso (dalla radice del repository, con le dipendenze installate):

    PI_SECRETARY_DEMO_DATABASE_URL="host=127.0.0.1 port=54329 dbname=secretary_demo user=... password=..." \
        python -m tests.demo_dashboard [--port 8099]

Il nome del database DEVE finire con `_demo`: il comando rifiuta qualsiasi altro database. Nessuna
credenziale reale, nessuna rete: token e indirizzi sono segnaposto. Accesso: utente `demo`, password `demo`.
Non e' un test (il nome non inizia con `test`), quindi `unittest discover` non lo esegue.
"""
import argparse
import json
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEMO_CONFIG = '''AGENT_NAME=Pi Secretary
TIMEZONE=Europe/Rome
LANGUAGE=it
DIGEST_TIME=21:00
TELEGRAM_BOT_TOKEN=123456:DEMO-TOKEN-NON-VALIDO
TELEGRAM_CHAT_ID=4242
OPENROUTER_API_KEY=sk-demo-non-valida
AGENT_MODEL=openrouter/auto
SECRETARY_MODE=observe
EMAIL1_NAME=Personale
EMAIL1_PROVIDER=custom
EMAIL1_ADDRESS=demo@example.invalid
EMAIL1_PASSWORD=demo-non-valida
EMAIL1_IMAP_HOST=imap.example.invalid
EMAIL1_SMTP_HOST=smtp.example.invalid
EMAIL2_NAME=Lavoro
EMAIL2_PROVIDER=custom
EMAIL2_ADDRESS=lavoro@example.invalid
EMAIL2_PASSWORD=demo-non-valida
EMAIL2_IMAP_HOST=imap.example.invalid
EMAIL2_SMTP_HOST=smtp.example.invalid
AI_BUDGET_DAILY_CALLS=400
AI_BUDGET_DAILY_TOKENS=2000000
AI_BUDGET_MAX_INFLIGHT=4
'''

SENDERS = ['Laura Bianchi', 'Marco Rossi', 'Studio Verdi', 'Banca Demo', 'Comune di Esempio', 'Gruppo Condominio',
           'Giulia Neri', 'Corriere Veloce', 'Assicurazioni Alfa', 'Mensa scolastica']
SUBJECTS = ['Conferma appuntamento', 'Fattura di ottobre', 'Riunione di condominio', 'Documenti mancanti',
            'Promemoria scadenza', 'Preventivo ristrutturazione', 'Ritiro pacco', 'Estratto conto',
            'Convocazione assemblea', 'Richiesta informazioni']
TOOLS = ['db_query', 'fs_read', 'mail_search', 'web_search', 'task_add', 'note_add', 'notify_urgent', 'email_draft']


def require_demo_database():
    import psycopg
    url = os.environ.get('PI_SECRETARY_DEMO_DATABASE_URL', '')
    name = psycopg.conninfo.conninfo_to_dict(url).get('dbname', '') if url else ''
    if not name.endswith('_demo'):
        sys.exit('Imposta PI_SECRETARY_DEMO_DATABASE_URL su un database il cui nome finisce con _demo.')
    return url


def seed(conn, now):
    rng = random.Random(7)
    tables = {'email': 'email_messages', 'telegram': 'telegram_messages', 'whatsapp': 'whatsapp_messages'}
    weights = {'email': 9, 'telegram': 5, 'whatsapp': 7}
    serial = 0
    for age in range(13, -1, -1):
        stamp = now - timedelta(days=age)
        for channel, table in tables.items():
            count = max(0, int(rng.gauss(weights[channel] * (1.4 if stamp.weekday() < 5 else 0.5), 2)))
            if age == 0:
                count = max(2, count // 2)
            for _ in range(count):
                serial += 1
                moment = stamp.replace(hour=rng.randint(7, 22), minute=rng.randint(0, 59), second=0, microsecond=0)
                if moment > now:
                    moment = now - timedelta(minutes=rng.randint(1, 90))
                direction = 'out' if rng.random() < 0.28 else 'in'
                row = conn.execute(f"""INSERT INTO {table}(ts,day,channel,sender,subject,body,source_id,direction,metadata)
                    VALUES (%s,(%s AT TIME ZONE 'Europe/Rome')::date,%s,%s,%s,%s,%s,%s,'{{}}') RETURNING id""",
                                   (moment, moment, channel, rng.choice(SENDERS), rng.choice(SUBJECTS),
                                    'Contenuto fittizio.', f'demo-{serial}', direction)).fetchone()
                if direction == 'in' and rng.random() < 0.8:
                    conn.execute("""INSERT INTO message_analysis(message_id,channel,summary,category,priority,needs_action,
                        needs_reply,notified,raw_verdict) VALUES (%s,%s,'Sintesi','generale','media',%s,%s,%s,'{}')""",
                                 (row[0], channel, rng.random() < 0.18, rng.random() < 0.2, rng.random() < 0.05))
    # Budget AI: 14 giorni di utilizzo, oggi piu' alto per mostrare le soglie.
    for age in range(13, -1, -1):
        day = (now - timedelta(days=age)).date()
        for index in range(rng.randint(30, 160) if age else 240):
            tokens = rng.randint(2000, 9000)
            conn.execute("""INSERT INTO secretary_ai_usage(id,usage_day,job_id,job_token,reserved_tokens,charged_tokens,
                state,total_tokens,provider_cost,scope,created_at,finished_at)
                VALUES (%s,%s,%s,'demo',%s,%s,'completed',%s,%s,%s,%s,%s)""",
                         (f'demo-{age}-{index}', day, index, tokens, tokens, tokens - 300, round(tokens / 1e6 * 1.1, 6),
                          rng.choice(['agent_owner', 'agent_inbox', 'agent_inbox']), now, now))
    # Esecuzioni dell'agente con strumenti.
    for number in range(46):
        started = now - timedelta(hours=number * 5 + rng.randint(0, 4))
        kind = rng.choice(['owner', 'inbox', 'inbox', 'digest'])
        state = 'failed' if rng.random() < 0.1 else ('running' if number == 0 else 'done')
        calls = rng.randint(1, 7)
        run = conn.execute("""INSERT INTO agent_runs(kind,turn_key,model,state,tool_calls,error,started_at,finished_at)
            VALUES (%s,%s,'openrouter/auto',%s,%s,%s,%s,%s) RETURNING id""",
                           (kind, f'demo-{number}', state, calls,
                            'Tempo scaduto del provider' if state == 'failed' else None, started,
                            None if state == 'running' else started + timedelta(seconds=rng.randint(4, 90)))).fetchone()
        for seq in range(1, calls + 1):
            tool = rng.choice(TOOLS)
            conn.execute("""INSERT INTO agent_tool_calls(run_id,seq,tool,args,ok,summary,duration_ms,at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                         (run[0], seq, tool, json.dumps({'query': 'esempio'}), rng.random() > 0.08,
                          f'{rng.randint(0, 40)} righe' if tool == 'db_query' else 'completato',
                          rng.randint(8, 900), started))
    for subject, state in (('Conferma riunione di giovedi', 'proposed'), ('Richiesta preventivo', 'sent'),
                           ('Sollecito pagamento', 'rejected')):
        conn.execute("""INSERT INTO agent_email_outbox(account,recipient,subject,body,state) VALUES
            ('demo@example.invalid','cliente@example.invalid',%s,%s,%s)""",
                     (subject, 'Buongiorno,\nconfermo quanto discusso.\nCordiali saluti', state))
    # Code di lavoro, attivita, appuntamenti, fatture.
    for kind, status, count, reason in (('analysis', 'done', 120, None), ('analysis', 'pending', 3, None),
                                        ('analysis', 'failed', 2, 'Provider AI non raggiungibile (timeout)'),
                                        ('media', 'done', 40, None), ('action', 'done', 18, None),
                                        ('knowledge', 'pending', 6, None), ('digest', 'done', 14, None)):
        for index in range(count):
            conn.execute("""INSERT INTO work_jobs(kind,key,payload,status,failure_reason,finished_at)
                VALUES (%s,%s,'{}',%s,%s,%s)""", (kind, f'demo:{kind}:{status}:{index}', status, reason,
                                                  now if status != 'pending' else None))
    for index, title in enumerate(['Rinnovare assicurazione auto', 'Pagare TARI', 'Prenotare revisione',
                                   'Inviare documenti al commercialista', 'Chiamare idraulico']):
        conn.execute("INSERT INTO tasks(source_message,title,deadline,status) VALUES (%s,%s,%s,'open')",
                     (1000 + index, title, now + timedelta(days=index * 3 + 1)))
    conn.execute("INSERT INTO tasks(source_message,title,status) VALUES (2000,'Vecchia attivita','done')")
    for index, title in enumerate(['Visita medica', 'Riunione condominio', 'Colloquio scuola']):
        stamp = now + timedelta(days=index + 1)
        conn.execute("INSERT INTO appointments(title,dt,sync_state) VALUES (%s,%s,%s)",
                     (title, f'{stamp:%Y-%m-%d} 1{index + 4}:30', 'pending' if index == 1 else 'synced'))
    for month in ('2026-10', '2026-09', '2026-08', '2026-07'):
        for entity in ('hypaz', 'alessio'):
            for index in range(rng.randint(2, 9)):
                conn.execute("""INSERT INTO invoices(entity,month,filename,path,content_hash)
                    VALUES (%s,%s,%s,%s,%s)""", (entity, month, f'fattura-{index}.pdf', f'/demo/{index}',
                                                  f'{entity}-{month}-{index}'))
            conn.execute("INSERT INTO monthly_zips(entity,month,path,sent) VALUES (%s,%s,'/demo.zip',%s) ON CONFLICT DO NOTHING",
                         (entity, month, month != '2026-10'))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--port', type=int, default=8099)
    parser.add_argument('--no-seed', action='store_true', help='non inserire dati se il database ne contiene gia')
    args = parser.parse_args()
    url = require_demo_database()
    root = Path(tempfile.mkdtemp(prefix='secretary-demo-'))
    (root / 'data').mkdir()
    config_file = root / 'secretary.env'
    config_file.write_text(DEMO_CONFIG)
    os.environ.update(PI_SECRETARY_CONFIG_FILE=str(config_file), DATA_DIR=str(root / 'data'), DATABASE_URL=url,
                      DASHBOARD_CREDENTIAL_FILE=str(root / 'dashboard.json'), DASHBOARD_BIND='127.0.0.1')
    from waitress import create_server
    from secretary.core import db, flows
    from secretary.dashboard import dashboard
    db.init()
    flows.init()
    with db._conn() as conn:
        if not (args.no_seed and conn.execute('SELECT count(*) FROM email_messages').fetchone()[0]):
            seed(conn, datetime.now(timezone.utc))
    (root / 'data' / 'uploads').mkdir(exist_ok=True)
    (root / 'data' / 'uploads' / 'contratto-demo.pdf').write_bytes(b'%PDF-1.4 demo')
    (root / 'data' / 'workspace').mkdir(exist_ok=True)
    print(f'Dashboard demo su http://127.0.0.1:{args.port}  (utente demo / password demo)  dati in {root}')
    create_server(dashboard.create_app({'username': 'demo', 'password': 'demo'}), host='127.0.0.1',
                  port=args.port, threads=4).run()


if __name__ == '__main__':
    main()
