import threading

import duckdb
import pytest

from lyst.store.db import Database, derive_key


def test_files_are_encrypted_at_rest(tmp_path):
    db = Database(tmp_path / "x.duckdb", name="x", master_key=b"k" * 32)
    db.execute("CREATE TABLE secret AS SELECT 'needle-4471' AS v")
    db.close()
    raw = (tmp_path / "x.duckdb").read_bytes()
    assert b"needle-4471" not in raw
    con = duckdb.connect()
    with pytest.raises(duckdb.Error):
        con.execute(f"ATTACH '{tmp_path / 'x.duckdb'}' AS a")
    with pytest.raises(duckdb.Error):
        con.execute(f"ATTACH '{tmp_path / 'x.duckdb'}' AS b (ENCRYPTION_KEY 'wrong')")
    con.execute(f"ATTACH '{tmp_path / 'x.duckdb'}' AS c (ENCRYPTION_KEY '{derive_key(b'k' * 32, 'x')}')")
    assert con.execute("SELECT v FROM c.secret").fetchone()[0] == "needle-4471"


def test_keys_differ_per_database():
    assert derive_key(b"m" * 32, "ws:a") != derive_key(b"m" * 32, "ws:b")


def test_concurrent_writes_are_serialized_and_reads_run_alongside(tmp_path):
    db = Database(tmp_path / "c.duckdb", name="c", master_key=b"k" * 32)
    db.execute("CREATE TABLE n (v INTEGER)")
    db.execute("INSERT INTO n VALUES (0)")

    def bump():
        for _ in range(25):
            db.write(lambda c: c.execute("UPDATE n SET v = (SELECT v FROM n) + 1"))
            db.scalar("SELECT v FROM n")

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert db.scalar("SELECT v FROM n") == 200
    db.close()


def test_failed_write_rolls_back(tmp_path):
    db = Database(tmp_path / "r.duckdb", name="r", master_key=b"k" * 32)
    db.execute("CREATE TABLE t (v INTEGER)")

    def bad(c):
        c.execute("INSERT INTO t VALUES (1)")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        db.write(bad)
    assert db.scalar("SELECT count(*) FROM t") == 0
    db.close()


def test_queries_cannot_reach_the_filesystem(tmp_path):
    db = Database(tmp_path / "f.duckdb", name="f", master_key=b"k" * 32)
    (tmp_path / "x.csv").write_text("a\n1\n")
    with pytest.raises(duckdb.Error):
        db.query(f"SELECT * FROM read_csv('{tmp_path / 'x.csv'}')")
    with pytest.raises(duckdb.Error):
        db.query("SET enable_external_access = true")
    db.close()


def test_second_process_cannot_open_a_live_file(tmp_path):
    import subprocess
    import sys

    db = Database(tmp_path / "l.duckdb", name="l", master_key=b"k" * 32)
    code = (f"import duckdb; c=duckdb.connect(); c.execute(\"ATTACH '{tmp_path / 'l.duckdb'}' AS a "
            f"(ENCRYPTION_KEY '{derive_key(b'k' * 32, 'l')}')\")")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode != 0 and "lock" in r.stderr.lower()
    db.close()
