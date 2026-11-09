"""PostgreSQL connection and additive, serialized schema initialization."""
import os
from pathlib import Path

def connect():
    import psycopg
    return psycopg.connect(os.environ['DATABASE_URL'], autocommit=True)

def jsonb(value):
    from psycopg.types.json import Jsonb
    return Jsonb(value)

def init_schema(conn):
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(4207)')
        conn.execute(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
        owner = os.environ.get('OWNER_TELEGRAM_ID', '').strip()
        if owner.isdigit() and int(owner) > 0:
            conn.execute("""INSERT INTO participants(telegram_id,role,active,miniapp_enabled)
                VALUES (%s,'owner',TRUE,TRUE)
                ON CONFLICT(telegram_id) DO UPDATE SET role='owner',active=TRUE,miniapp_enabled=TRUE""",
                (int(owner),))

def log(service, message):
    print(f'[{service}] {message}', flush=True)


def mark_topics(conn, topics):
    """Call in the same transaction as fact/admission changes, in stable lock order.

    Do not reset paid attempts/cooldown here: only an actually new input snapshot
    may reset them. Redundant dirty marks must not circumvent the retry limit.
    """
    names = sorted({t.strip()[:200] for t in topics if isinstance(t, str) and t.strip()})
    for topic in names:
        conn.execute('''INSERT INTO topic_refresh(topic) VALUES (%s)
            ON CONFLICT(topic) DO UPDATE SET revision=topic_refresh.revision+1,
            state='pending',updated_at=now()''', (topic,))
        conn.execute('UPDATE conclusions SET is_current=FALSE WHERE topic=%s AND is_current', (topic,))

def start_run(conn, service):
    identifier=conn.execute("INSERT INTO runs(service,status) VALUES (%s,'running') RETURNING id", (service,)).fetchone()[0]
    log(service,f'Прогон {identifier} начат')
    return identifier

def finish_run(conn, service, run_id, items, note):
    conn.execute("UPDATE runs SET status='finished',finished_at=now(),items=%s,note=%s WHERE id=%s", (items,note,run_id))
    log(service,f'Прогон {run_id} завершён: {note}')

def progress_run(conn, service, run_id, items, note):
    conn.execute("UPDATE runs SET items=%s,note=%s WHERE id=%s", (items,note,run_id))
    log(service,f'Прогон {run_id}: {note}')

def fail_run(conn, service, run_id, error):
    conn.execute("UPDATE runs SET status='failed',finished_at=now(),note=%s WHERE id=%s", (type(error).__name__,run_id))
    log(service,f'Прогон {run_id}: {type(error).__name__}')
