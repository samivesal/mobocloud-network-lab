import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import random
import sqlite3
import time

from cryptography.fernet import Fernet

SCHEMA = """
CREATE TABLE IF NOT EXISTS configs(id INTEGER PRIMARY KEY, name TEXT NOT NULL, product TEXT NOT NULL,
 country TEXT NOT NULL, protocol TEXT NOT NULL, transport TEXT NOT NULL, secret BLOB NOT NULL,
 fingerprint TEXT UNIQUE NOT NULL, interval INTEGER DEFAULT 0, due REAL DEFAULT 0,
 health TEXT DEFAULT 'unknown', streak INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, config INTEGER REFERENCES configs(id),
 agent TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, created INTEGER NOT NULL,
 reserved INTEGER DEFAULT 0, error TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending ON jobs(config) WHERE state IN ('queued','running');
CREATE INDEX IF NOT EXISTS jobs_date ON jobs(created);
CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY, job INTEGER REFERENCES jobs(id),
 created INTEGER NOT NULL, baseline TEXT NOT NULL, result TEXT NOT NULL, confidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents(id INTEGER PRIMARY KEY, config INTEGER, opened INTEGER, closed INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS one_incident ON incidents(config) WHERE closed IS NULL;
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY,incident INTEGER,event TEXT,state TEXT DEFAULT 'pending',
 attempts INTEGER DEFAULT 0,due REAL DEFAULT 0, UNIQUE(incident,event));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at INTEGER, action TEXT,object_id INTEGER);
PRAGMA user_version=1;
"""
MAX_PAYLOAD_BYTES = 10_000_000


def speed_reservation(size):
    return size*2+262144


class Store:
    def __init__(self, path, key, agent="local"):
        self.path=Path(path)
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.cipher=Fernet(key)
        self.hashkey=key
        self.agent=agent
        with self.db() as db:
            db.execute("pragma journal_mode=wal")
            if db.execute("pragma user_version").fetchone()[0] > 1:
                raise RuntimeError("unsupported database version")
            db.executescript(SCHEMA)
            from .schedules import setup
            setup(db)
            from .reports import setup as setup_reports
            setup_reports(db)
            from .feeds import setup as setup_feeds
            setup_feeds(db)

    @contextlib.contextmanager
    def db(self, write=False):
        conn=sqlite3.connect(self.path,timeout=10)
        conn.row_factory=sqlite3.Row
        try:
            conn.execute("pragma foreign_keys=on")
            if write:
                conn.execute("begin immediate")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def rows(self,query,args=()):
        with self.db() as db:
            return [dict(r) for r in db.execute(query,args)]

    def add(self,db,out,name,product,country):
        raw=json.dumps(out,sort_keys=True).encode()
        fp=hmac.new(self.hashkey,raw,hashlib.sha256).hexdigest()
        db.execute("""insert into configs(name,product,country,protocol,transport,secret,fingerprint)
            values(?,?,?,?,?,?,?) on conflict(fingerprint) do nothing""",(name,product,country,out["protocol"],
            out.get("streamSettings",{}).get("network","tcp"),self.cipher.encrypt(raw),fp))
        return db.execute("select id from configs where fingerprint=?",(fp,)).fetchone()[0]

    def enqueue(self,db,cid,kind,*,subscription_test=False):
        if os.getenv("LAB_LIVE")!="1":
            raise ValueError("live_disabled")
        cfg=db.execute("select enabled from configs where id=?",(cid,)).fetchone()
        if not cfg or not cfg[0]:
            raise ValueError("config_disabled_or_missing")
        reserved=0
        now=int(time.time())
        if kind!="check":
            if os.getenv("LAB_SPEED")!="1" or not os.getenv("LAB_"+kind.upper()+"_URL"):
                raise ValueError("speed_disabled")
            if not subscription_test:
                reserved=speed_reservation(amount(kind))
                used,own=db.execute("""select coalesce(sum(reserved),0),coalesce(sum(case when config=? then reserved end),0)
                     from jobs where created>=? and not subscription_test""",(cid,now-now%86400)).fetchone()
                if used+reserved>int(os.getenv("LAB_DAY_CAP","20000000")) or own+reserved>int(os.getenv("LAB_CONFIG_CAP","10000000")):
                    raise ValueError("budget_exhausted")
        return db.execute("""insert into jobs(config,agent,kind,state,created,reserved,subscription_test)
            values(?,?,?,'queued',?,?,?)""",(cid,self.agent,kind,now,reserved,subscription_test)).lastrowid

    def record(self,db,job,base,result):
        limit=int(os.getenv("LAB_BASELINE_MAX_MS","0"))
        baseline_ok=base["ok"] and (not limit or base.get("total_ms",0)<=limit)
        confidence="valid" if baseline_ok else "degraded_baseline"
        now=int(time.time())
        db.execute("insert into samples(job,created,baseline,result,confidence) values(?,?,?,?,?)",
                   (job["id"],now,json.dumps(base),json.dumps(result),confidence))
        if job["kind"]!="check":
            return
        cid=job["config"]
        streak=db.execute("select streak from configs where id=?",(cid,)).fetchone()[0]
        streak=max(0,streak)+1 if result["ok"] else min(0,streak)-1 if baseline_ok else 0
        opened=db.execute("select id from incidents where config=? and closed is null",(cid,)).fetchone()
        health="inconclusive" if streak==0 else "degraded"
        event=None
        if streak>=int(os.getenv("LAB_RECOVER","2")):
            health="healthy"
            if opened:
                incident=opened[0]
                db.execute("update incidents set closed=? where id=?",(now,incident))
                event="recovery"
        elif streak<=-int(os.getenv("LAB_CONFIRM","3")) and not opened:
            incident=db.execute("insert into incidents(config,opened) values(?,?)",(cid,now)).lastrowid
            event="outage"
        if (opened and not event) or event=="outage":
            health="unavailable"
        if event and os.getenv("LAB_TELEGRAM")=="1":
            db.execute("insert into alerts(incident,event) values(?,?)",(incident,event))
        db.execute("update configs set streak=?,health=? where id=?",(streak,health,cid))


def amount(kind):
    size=int(os.getenv("LAB_"+kind.upper()+"_BYTES","1000000" if kind=="upload" else "5000000"))
    if not 1<=size<=MAX_PAYLOAD_BYTES:
        raise ValueError("invalid_size")
    return size
