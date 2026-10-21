"""Explicit, resumable migration; never invoked by application startup."""
import argparse

from secretary.archive import email_attachments


def migrate(conn, limit=None):
    """One locked row/transaction at a time; failures leave the blob intact."""
    count = 0
    last_id = 0
    while limit is None or count < limit:
        with conn.transaction():
            row = conn.execute("""SELECT id,data FROM attachments
                WHERE channel='email' AND data IS NOT NULL AND id>%s
                ORDER BY id LIMIT 1 FOR UPDATE""", (last_id,)).fetchone()
            if row is None:
                break
            attachment_id, blob = row
            data = bytes(blob)
            path = email_attachments.persist(data)
            # Verify again immediately before the transactional destructive update.
            email_attachments.verify(path, data)
            conn.execute("""UPDATE attachments SET path=%s,data=NULL
                WHERE id=%s AND channel='email' AND data IS NOT NULL""",
                (path, attachment_id))
        last_id = attachment_id
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Explicitly connect and migrate blobs')
    parser.add_argument('--limit', type=int, help='Maximum rows to migrate this run')
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    if not args.apply:
        print('No database accessed. Pass --apply to migrate; back up DB and attachment files first.')
        return
    from secretary.core import db
    with db._conn() as conn:
        print(f'Migrated {migrate(conn, args.limit)} email attachments.')


if __name__ == '__main__':
    main()