from typing import Tuple
import psycopg
import psycopg2
import psutil
import os
import signal
import time

from psycopg_pool import ConnectionPool

SHUTDOWN_TIMEOUT = 5

PGCAT_HOST = "127.0.0.1"
PGCAT_PORT = "6432"


def pgcat_start():
    pg_cat_send_signal(signal.SIGTERM)
    os.system("./target/debug/pgcat .circleci/pgcat.toml &")
    time.sleep(2)


def pg_cat_send_signal(signal: signal.Signals):
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            if "pgcat" == proc.name():
                os.kill(proc.pid, signal)
    except Exception as e:
        # The process can be gone when we send this signal
        print(e)

    if signal == signal.SIGTERM:
        # Returns 0 if pgcat process exists
        time.sleep(2)
        if not os.system('pgrep pgcat'):
            raise Exception("pgcat not closed after SIGTERM")


def connect_db(
    autocommit: bool = True,
    admin: bool = False,
) -> Tuple[psycopg2.extensions.connection, psycopg2.extensions.cursor]:

    if admin:
        user = "admin_user"
        password = "admin_pass"
        db = "pgcat"
    else:
        user = "sharding_user"
        password = "sharding_user"
        db = "sharded_db"

    conn = psycopg2.connect(
        f"postgres://{user}:{password}@{PGCAT_HOST}:{PGCAT_PORT}/{db}?application_name=testing_pgcat",
        connect_timeout=2,
    )
    conn.autocommit = autocommit
    cur = conn.cursor()

    return (conn, cur)


def cleanup_conn(conn: psycopg2.extensions.connection, cur: psycopg2.extensions.cursor):
    cur.close()
    conn.close()


def test_normal_db_access():
    pgcat_start()
    conn, cur = connect_db(autocommit=False)
    cur.execute("SELECT 1")
    res = cur.fetchall()
    print(res)
    cleanup_conn(conn, cur)


def test_psycopg() -> None:
    """Test using psycopg aka psycopg3."""
    pgcat_start()
    dsn = f"postgres://sharding_user:sharding_user@{PGCAT_HOST}:{PGCAT_PORT}/sharded_db?application_name=testing_pgcat"

    # test pipeline operation
    # see https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html#pipeline-mode
    with ConnectionPool(dsn, timeout=2, kwargs={'autocommit': True, 'prepare_threshold': None}, max_size=1, min_size=1) as pool:
        with pool.connection() as conn:
            conn.execute("DROP TABLE IF EXISTS mytable")
            conn.execute("CREATE TABLE mytable (data TEXT)")
            with conn.pipeline() as p, conn.cursor() as cur:
                try:
                    cur.execute("INSERT INTO mytable (data) VALUES (%s)", ["one"])
                    cur.execute("INSERT INTO no_such_table (data) VALUES (%s)", ["two"])
                    conn.execute("INSERT INTO mytable (data) VALUES (%s)", ["three"])
                    p.sync()
                except psycopg.errors.UndefinedTable:
                    pass
                cur.execute("INSERT INTO mytable (data) VALUES (%s)", ["four"])
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM mytable")
                res = cur.fetchall()
                assert res == [("four",)]
    
    # test prepared statements
    with ConnectionPool(dsn, timeout=2, kwargs={'autocommit': True, 'prepare_threshold': 0}, max_size=1, min_size=1) as pool:
        # run the query multiple times to exercise prepared statements
        with pool.connection() as conn:
            for _ in range(10):
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    res = cur.fetchall()
                    assert res == [(1,)]

    # test copy operation
    with ConnectionPool(dsn, timeout=2, kwargs={'autocommit': True, 'prepare_threshold': None}, max_size=1, min_size=1) as pool:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                with cur.copy('COPY (SELECT 1) TO STDOUT') as copy:
                    x = -1
                    for (x, row) in enumerate(copy.rows()):
                        assert row == ('1',), row
                    assert x == 0

    pg_cat_send_signal(signal.SIGTERM)


def test_admin_db_access():
    conn, cur = connect_db(admin=True)

    cur.execute("SHOW POOLS")
    res = cur.fetchall()
    print(res)
    cleanup_conn(conn, cur)


def test_shutdown_logic():

    # - - - - - - - - - - - - - - - - - -
    # NO ACTIVE QUERIES SIGINT HANDLING

    # Start pgcat
    pgcat_start()

    # Create client connection and send query (not in transaction)
    conn, cur = connect_db()

    cur.execute("BEGIN;")
    cur.execute("SELECT 1;")
    cur.execute("COMMIT;")

    # Send sigint to pgcat
    pg_cat_send_signal(signal.SIGINT)
    time.sleep(1)

    # Check that any new queries fail after sigint since server should close with no active transactions
    try:
        cur.execute("SELECT 1;")
    except psycopg2.OperationalError as e:
        pass
    else:
        # Fail if query execution succeeded
        raise Exception("Server not closed after sigint")

    cleanup_conn(conn, cur)
    pg_cat_send_signal(signal.SIGTERM)

    # - - - - - - - - - - - - - - - - - -
    # NO ACTIVE QUERIES ADMIN SHUTDOWN COMMAND

    # Start pgcat
    pgcat_start()

    # Create client connection and begin transaction
    conn, cur = connect_db()
    admin_conn, admin_cur = connect_db(admin=True)

    cur.execute("BEGIN;")
    cur.execute("SELECT 1;")
    cur.execute("COMMIT;")

    # Send SHUTDOWN command pgcat while not in transaction
    admin_cur.execute("SHUTDOWN;")
    time.sleep(1)

    # Check that any new queries fail after SHUTDOWN command since server should close with no active transactions
    try:
        cur.execute("SELECT 1;")
    except psycopg2.OperationalError as e:
        pass
    else:
        # Fail if query execution succeeded
        raise Exception("Server not closed after sigint")

    cleanup_conn(conn, cur)
    cleanup_conn(admin_conn, admin_cur)
    pg_cat_send_signal(signal.SIGTERM)

    # - - - - - - - - - - - - - - - - - -
    # HANDLE TRANSACTION WITH SIGINT

    # Start pgcat
    pgcat_start()

    # Create client connection and begin transaction
    conn, cur = connect_db()

    cur.execute("BEGIN;")
    cur.execute("SELECT 1;")

    # Send sigint to pgcat while still in transaction
    pg_cat_send_signal(signal.SIGINT)
    time.sleep(1)

    # Check that any new queries succeed after sigint since server should still allow transaction to complete
    try:
        cur.execute("SELECT 1;")
    except psycopg2.OperationalError as e:
        # Fail if query fails since server closed
        raise Exception("Server closed while in transaction", e.pgerror)

    cleanup_conn(conn, cur)
    pg_cat_send_signal(signal.SIGTERM)

    # - - - - - - - - - - - - - - - - - -
    # HANDLE TRANSACTION WITH ADMIN SHUTDOWN COMMAND

    # Start pgcat
    pgcat_start()

    # Create client connection and begin transaction
    conn, cur = connect_db()
    admin_conn, admin_cur = connect_db(admin=True)

    cur.execute("BEGIN;")
    cur.execute("SELECT 1;")

    # Send SHUTDOWN command pgcat while still in transaction
    admin_cur.execute("SHUTDOWN;")
    if admin_cur.fetchall()[0][0] != "t":
        raise Exception("PgCat unable to send signal")
    time.sleep(1)

    # Check that any new queries succeed after SHUTDOWN command since server should still allow transaction to complete
    try:
        cur.execute("SELECT 1;")
    except psycopg2.OperationalError as e:
        # Fail if query fails since server closed
        raise Exception("Server closed while in transaction", e.pgerror)

    cleanup_conn(conn, cur)
    cleanup_conn(admin_conn, admin_cur)
    pg_cat_send_signal(signal.SIGTERM)

    # - - - - - - - - - - - - - - - - - -
    # NO NEW NON-ADMIN CONNECTIONS DURING SHUTDOWN
    # Start pgcat
    pgcat_start()

    # Create client connection and begin transaction
    transaction_conn, transaction_cur = connect_db()

    transaction_cur.execute("BEGIN;")
    transaction_cur.execute("SELECT 1;")

    # Send sigint to pgcat while still in transaction
    pg_cat_send_signal(signal.SIGINT)
    time.sleep(1)

    start = time.perf_counter()
    try:
        conn, cur = connect_db()
        cur.execute("SELECT 1;")
        cleanup_conn(conn, cur)
    except psycopg2.OperationalError as e:
        time_taken = time.perf_counter() - start
        if time_taken > 0.1:
            raise Exception(
                "Failed to reject connection within 0.1 seconds, got", time_taken, "seconds")
        pass
    else:
        raise Exception("Able connect to database during shutdown")

    cleanup_conn(transaction_conn, transaction_cur)
    pg_cat_send_signal(signal.SIGTERM)

    # - - - - - - - - - - - - - - - - - -
    # ALLOW NEW ADMIN CONNECTIONS DURING SHUTDOWN
    # Start pgcat
    pgcat_start()

    # Create client connection and begin transaction
    transaction_conn, transaction_cur = connect_db()

    transaction_cur.execute("BEGIN;")
    transaction_cur.execute("SELECT 1;")

    # Send sigint to pgcat while still in transaction
    pg_cat_send_signal(signal.SIGINT)
    time.sleep(1)

    try:
        conn, cur = connect_db(admin=True)
        cur.execute("SHOW DATABASES;")
        cleanup_conn(conn, cur)
    except psycopg2.OperationalError as e:
        raise Exception(e)

    cleanup_conn(transaction_conn, transaction_cur)
    pg_cat_send_signal(signal.SIGTERM)

    # - - - - - - - - - - - - - - - - - -
    # ADMIN CONNECTIONS CONTINUING TO WORK AFTER SHUTDOWN
    # Start pgcat
    pgcat_start()

    # Create client connection and begin transaction
    transaction_conn, transaction_cur = connect_db()
    transaction_cur.execute("BEGIN;")
    transaction_cur.execute("SELECT 1;")

    admin_conn, admin_cur = connect_db(admin=True)
    admin_cur.execute("SHOW DATABASES;")

    # Send sigint to pgcat while still in transaction
    pg_cat_send_signal(signal.SIGINT)
    time.sleep(1)

    try:
        admin_cur.execute("SHOW DATABASES;")
    except psycopg2.OperationalError as e:
        raise Exception("Could not execute admin command:", e)

    cleanup_conn(transaction_conn, transaction_cur)
    cleanup_conn(admin_conn, admin_cur)
    pg_cat_send_signal(signal.SIGTERM)

    # - - - - - - - - - - - - - - - - - -
    # HANDLE SHUTDOWN TIMEOUT WITH SIGINT

    # Start pgcat
    pgcat_start()

    # Create client connection and begin transaction, which should prevent server shutdown unless shutdown timeout is reached
    conn, cur = connect_db()

    cur.execute("BEGIN;")
    cur.execute("SELECT 1;")

    # Send sigint to pgcat while still in transaction
    pg_cat_send_signal(signal.SIGINT)

    # pgcat shutdown timeout is set to SHUTDOWN_TIMEOUT seconds, so we sleep for SHUTDOWN_TIMEOUT + 1 seconds
    time.sleep(SHUTDOWN_TIMEOUT + 1)

    # Check that any new queries succeed after sigint since server should still allow transaction to complete
    try:
        cur.execute("SELECT 1;")
    except psycopg2.OperationalError as e:
        pass
    else:
        # Fail if query execution succeeded
        raise Exception("Server not closed after sigint and expected timeout")

    cleanup_conn(conn, cur)
    pg_cat_send_signal(signal.SIGTERM)

    # - - - - - - - - - - - - - - - - - -


test_normal_db_access()
test_psycopg()
test_admin_db_access()
test_shutdown_logic()
