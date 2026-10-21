"""Correzione retroattiva: aggiorna ts/day della tabella communications
con la data REALE delle email (header Date), rileggendole dalle caselle.

Ottimizzato: UNA sola connessione per casella con SEARCH SINCE
sull'intera finestra (non un ciclo per giorno).

Uso:
    python3 -m secretary.maintenance.fix_communication_dates [--dry-run] [--days N]
"""
import sys
from datetime import datetime, timedelta

import email as email_lib

from secretary.core import config, db
from secretary.connectors import email as email_conn


def main() -> None:
    dry = "--dry-run" in sys.argv
    days = 7  # lookback di default: SOLO gli ultimi 7 giorni, mai oltre
    for i, a in enumerate(sys.argv):
        if a == "--days" and i + 1 < len(sys.argv):
            days = min(int(sys.argv[i + 1]), 31)  # hard cap: mai più di 31
    db.init()

    start = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    print(f"Rileggo le email dal {start} a oggi da {len(config.EMAIL_ACCOUNTS)} caselle "
          f"(una connessione per casella)...")

    # mappa (sender, subject) -> data reale (prima occorrenza = più vecchia)
    date_map = {}
    for account in config.EMAIL_ACCOUNTS:
        n = 0
        try:
            imap = email_conn._connect(account)
            imap.select("INBOX", readonly=True)
            _, data = imap.search(None, f'(SINCE "{start}")')
            uids = data[0].split()
            # scarica SOLO gli header necessari (Date/From/Subject):
            # molto più veloce del RFC822 completo (niente corpi/allegati)
            for uid in uids:
                try:
                    _, hdr = imap.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)])")
                    raw = hdr[0][1]
                    msg = email_lib.message_from_bytes(raw)
                    sender = email_conn._decode(msg.get("From"))
                    subject = email_conn._decode(msg.get("Subject"))
                    real = email_conn.parse_email_datetime(msg.get("Date", ""))
                except Exception:  # noqa: BLE001
                    continue
                if not real:
                    continue
                from_addr = email_lib.utils.parseaddr(sender)[1] or sender
                k = ((from_addr or "").lower(), (subject or "").strip())
                if k not in date_map:
                    date_map[k] = real
                n += 1
            imap.logout()
        except Exception as exc:  # noqa: BLE001
            print(f"  [!] {account['name']}: {exc}")
        print(f"  • {account['name']}: {n} email lette (solo header)")

    print(f"\nTotale email mappate: {len(date_map)}")

    # aggiorna communications: match su (sender, subject)
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT id, sender, subject FROM communications""").fetchall()
        updated = 0
        for comm_id, sender, subject in rows:
            k = ((sender or "").lower(), (subject or "").strip())
            real = date_map.get(k)
            if not real:
                continue
            if dry:
                print(f"  [dry] id={comm_id}: {real} — {k[1][:50]}")
            else:
                conn.execute(
                    """UPDATE communications
                       SET ts = %(ts)s::timestamptz, day = %(day)s::date
                       WHERE id = %(id)s""",
                    {"ts": real, "day": real[:10], "id": comm_id})
            updated += 1
    print(f"\n{'[DRY-RUN] ' if dry else ''}Comunicazioni aggiornate: {updated} su {len(rows)}")


if __name__ == "__main__":
    main()