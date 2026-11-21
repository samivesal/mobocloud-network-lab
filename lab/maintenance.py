"""Consistent online SQLite backup and offline restore to a fresh path."""
import argparse
import contextlib
import os
from pathlib import Path
import sqlite3


def copy_db(source,dest):
    if not source.is_file():
        raise ValueError("source missing")
    fd=os.open(dest,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    os.close(fd)
    try:
        with contextlib.closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro",uri=True)) as a,contextlib.closing(sqlite3.connect(dest)) as b:
            a.backup(b)
            if b.execute("pragma integrity_check").fetchone()[0]!="ok":
                raise ValueError("integrity check failed")
            b.execute("select id from configs limit 1")
    except BaseException:
        dest.unlink()
        raise


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=["backup","restore"])
    parser.add_argument("file",type=Path)
    parser.add_argument("--database",type=Path,default=Path(os.getenv("LAB_DATA","var/data"))/"lab.sqlite")
    args=parser.parse_args()
    if args.action=="backup":
        copy_db(args.database,args.file)
    else:
        args.database.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        copy_db(args.file,args.database)


if __name__=="__main__":main()
