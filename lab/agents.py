"""Authenticated relay between independently routed installations."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Sample(BaseModel):
    model_config=ConfigDict(extra="forbid",allow_inf_nan=False)
    method:str=Field(pattern=r"^[a-f0-9]{16}$")
    source_id:int=Field(gt=0)
    config:int=Field(gt=0)
    at:int=Field(gt=0)
    ok:bool
    ms:float=Field(ge=0,le=600000)
    kind:Literal["check","download","upload"]
    confidence:Literal["valid","degraded_baseline"]


class Batch(BaseModel):
    model_config=ConfigDict(extra="forbid")
    samples:list[Sample]=Field(max_length=100)


def tables(db):
    db.executescript("""
    create table if not exists remote_agents(name text primary key, heartbeat integer);
    create table if not exists remote_samples(agent text, source_id integer, config integer references configs(id),
      at integer, ok integer, ms real, kind text, confidence text, method text, primary key(agent,source_id));
    create index if not exists remote_by_time on remote_samples(config,at);
    """)


def signature(key,stamp,body):
    return hmac.new(key.encode(),stamp.encode()+b"\n"+body,hashlib.sha256).hexdigest()


def register(app,store):
    with store.db() as db:
        tables(db)

    @app.post("/api/v1/agent-batches/{agent}")
    async def receive(agent:str,req:Request):
        path=os.getenv("LAB_AGENT_REGISTRY")
        entry=json.loads(Path(path).read_text()).get(agent) if path else None
        if not entry:
            raise HTTPException(403,"unknown_agent")
        stamp=req.headers.get("x-lab-time","")
        body=await req.body()
        if (not stamp.isascii() or not stamp.isdigit() or len(stamp)>12 or abs(time.time()-int(stamp))>60
            or not hmac.compare_digest(signature(entry["key"],stamp,body),req.headers.get("x-signature",""))):
            raise HTTPException(403,"agent_signature")
        try:
            batch=Batch.model_validate_json(body)
            with store.db(True) as db:
                for r in batch.samples:
                    cid=entry["configs"][str(r.config)]
                    db.execute("insert into remote_samples values(?,?,?,?,?,?,?,?,?) on conflict(agent,source_id) do nothing",
                               (agent,r.source_id,cid,r.at,r.ok,r.ms,r.kind,r.confidence,r.method))
                db.execute("insert into remote_agents values(?,?) on conflict(name) do update set heartbeat=excluded.heartbeat",(agent,int(stamp)))
        except (KeyError,ValidationError,sqlite3.IntegrityError):
            raise HTTPException(422,"invalid_agent_batch") from None
        return {"accepted":len(batch.samples)}


def relay():
    parser=argparse.ArgumentParser(description="Send measurements and heartbeat from this independently routed lab")
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args()
    target=os.environ["LAB_HUB"]
    if urlsplit(target).scheme!="https":
        raise SystemExit("LAB_HUB requires HTTPS")
    directory=Path(os.getenv("LAB_DATA","var/data"))
    cursor=directory/"relay.cursor"
    agent=os.environ["LAB_AGENT"]
    if not agent.isascii() or not agent.replace("-","").isalnum():
        raise SystemExit("Invalid agent name")
    while True:
        last=int(cursor.read_text()) if cursor.exists() else 0
        with sqlite3.connect((directory/"lab.sqlite").resolve().as_uri()+"?mode=ro",uri=True) as db:
            rows=db.execute("""select s.id,j.config,s.created,s.result,j.kind,s.confidence from samples s
                join jobs j on j.id=s.job where s.id>? order by s.id limit 100""",(last,)).fetchall()
        samples=[]
        for sid,cid,at,raw,kind,confidence in rows:
            r=json.loads(raw)
            if not r.get("method"):
                continue
            samples.append(dict(method=r["method"],source_id=sid,config=cid,at=at,ok=r["ok"],ms=r["total_ms"],kind=kind,confidence=confidence))
        body=json.dumps({"samples":samples}).encode()
        stamp=str(int(time.time()))
        try:
            with httpx.Client(timeout=10,trust_env=False) as client:
                resp=client.post(target.rstrip("/")+"/api/v1/agent-batches/"+agent,content=body,headers={
                    "content-type":"application/json","x-lab-request":"1","x-lab-time":stamp,
                    "x-signature":signature(os.environ["LAB_RELAY_KEY"],stamp,body)})
                resp.raise_for_status()
            if rows:
                tmp=cursor.with_suffix(".new")
                tmp.write_text(str(rows[-1][0]))
                tmp.replace(cursor)
        except httpx.HTTPError:
            if args.once:
                raise SystemExit("Hub delivery failed")
        if args.once:return
        time.sleep(30)


if __name__=="__main__":
    relay()
