import argparse
import base64
import collections
import contextlib
import csv
import fcntl
import getpass
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import statistics
import threading
import time
from typing import Literal

from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .config import parse_document
from .store import Store
from .worker import Worker

ROOT=Path(__file__).resolve().parent.parent
DATA=Path(os.getenv("LAB_DATA","var/data"))
KEY=Path(os.getenv("LAB_KEY_DIR","var/keys"))
PREFIX="/api/v1"
PUBLIC="id,name,product,country,protocol,transport,interval,enabled,health"


def digest(password,salt):
    return hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1)


def initialize():
    os.umask(0o077)
    KEY.mkdir(parents=True,exist_ok=True,mode=0o700)
    password=getpass.getpass("New administrator password (12+ characters): ")
    if len(password)<12:
        raise SystemExit("Use at least 12 characters")
    salt=secrets.token_bytes(16)
    with (KEY/"identity.json").open("x") as f:
        json.dump(dict(salt=salt.hex(),password=digest(password,salt).hex(),key=Fernet.generate_key().decode()),f)


class Import(BaseModel):
    text:str=Field(min_length=1,max_length=90000)
    name:str=Field(min_length=1,max_length=80)
    product:Literal["Pro","Basic","Other"]="Pro"
    country:str=Field(default="",max_length=60)


class Edit(BaseModel):
    enabled:bool
    interval:int=Field(ge=0,le=2592000)


class Run(BaseModel):
    config:int
    kind:Literal["check","upload","download"]="check"


class Login(BaseModel):
    password:str=Field(max_length=512)


def create():
    from .policy import validate
    validate()
    os.umask(0o077)
    identity=json.loads((KEY/"identity.json").read_text())
    store=Store(DATA/"lab.sqlite",identity["key"].encode(),os.getenv("LAB_AGENT","local"))
    worker=Worker(store)
    sessions={}
    attempts=collections.deque()

    @contextlib.asynccontextmanager
    async def lifetime(app):
        with (DATA/"worker.lock").open("a") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            thread=threading.Thread(target=worker.loop)
            thread.start()
            try:
                yield
            finally:
                worker.stop.set()
                thread.join()

    app=FastAPI(title="Mobocloud Network Lab",lifespan=lifetime,openapi_url=None,docs_url=None,redoc_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid(req,exc):
        return JSONResponse({"detail":"invalid_input"},422)

    @app.middleware("http")
    async def boundary(req,call_next):
        if req.method not in ("GET","HEAD"):
            if req.headers.get("x-lab-request")!="1":
                return JSONResponse({"detail":"request_header_required"},403)
            try:
                length=int(req.headers.get("content-length","-1"))
            except ValueError:
                length=-1
            if not 0<=length<=131072:
                return JSONResponse({"detail":"invalid_length"},413)
        req_id=secrets.token_hex(8)
        resp=await call_next(req)
        resp.headers["X-Request-ID"]=req_id
        resp.headers.update({"Cache-Control":"no-store","X-Content-Type-Options":"nosniff","Referrer-Policy":"no-referrer",
                             "Content-Security-Policy":"default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"})
        return resp

    def auth(req:Request):
        if sessions.get(req.cookies.get("lab_session"),0)<time.time():
            raise HTTPException(401,"login_required")

    from fastapi import APIRouter
    api=APIRouter(prefix=PREFIX,dependencies=[Depends(auth)])

    @app.post(PREFIX+"/login")
    async def login(data:Login,response:Response):
        now=time.time()
        while attempts and attempts[0]<now-60:
            attempts.popleft()
        if len(attempts)>=8:
            raise HTTPException(429,"retry_later")
        if not hmac.compare_digest(digest(data.password,bytes.fromhex(identity["salt"])).hex(),identity["password"]):
            attempts.append(now)
            raise HTTPException(401,"invalid_login")
        for k,v in list(sessions.items()):
            if v<=now:
                del sessions[k]
        token=secrets.token_urlsafe(32)
        sessions[token]=now+28800
        response.set_cookie("lab_session",token,httponly=True,samesite="strict",max_age=28800,secure=os.getenv("LAB_HTTPS")=="1")
        return {"ok":True}

    @api.post("/logout")
    def logout(req:Request,response:Response):
        sessions.pop(req.cookies.get("lab_session"),None)
        response.delete_cookie("lab_session")
        return {"ok":True}

    @app.get("/health")
    def health():
        store.rows("select 1")
        return {"worker_alive":time.time()-worker.pulse<120}

    @api.get("/openapi.json")
    def spec():
        return app.openapi()

    @api.get("/configurations")
    def configs(offset:int=Query(0,ge=0),search:str=Query("",max_length=80)):
        return store.rows("select "+PUBLIC+" from configs where instr(lower(name||' '||product||' '||country||' '||protocol||' '||transport||' '||health),lower(?))>0 order by id desc limit 100 offset ?",(search,offset))

    @api.post("/configurations")
    def add(data:Import):
        try:
            outs=parse_document(data.text)
        except (ValueError,TypeError,AttributeError,KeyError,IndexError,UnicodeError):
            raise HTTPException(422,"invalid_or_unsupported_configuration") from None
        with store.db(True) as db:
            ids=[]
            for i,out in enumerate(outs):
                cid=store.add(db,out,data.name if len(outs)==1 else f"{data.name} {i+1}",data.product,data.country)
                ids.append(cid)
                db.execute("insert into audit(at,action,object_id) values(unixepoch(),'import',?)",(cid,))
        return {"ids":ids}

    @api.patch("/configurations/{cid}")
    def edit(cid:int,data:Edit):
        if data.interval and (data.interval<60 or os.getenv("LAB_SCHEDULE")!="1"):
            raise HTTPException(409,"scheduling_disabled_or_interval_too_short")
        with store.db(True) as db:
            if not db.execute("update configs set enabled=?,interval=? where id=?",(data.enabled,data.interval,cid)).rowcount:
                raise HTTPException(404,"missing")
            db.execute("insert into audit(at,action,object_id) values(unixepoch(),'edit',?)",(cid,))
        return {"ok":True}

    @api.put("/configurations/{cid}/schedules/{kind}")
    def heavy_schedule(cid:int,kind:Literal["upload","download"],seconds:int=Query(ge=0,le=2592000)):
        from .schedules import configure
        with store.db(True) as db:
            if not db.execute("select id from configs where id=?",(cid,)).fetchone():
                raise HTTPException(404,"missing")
            try:
                configure(db,cid,kind,seconds)
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from None
            db.execute("insert into audit(at,action,object_id) values(unixepoch(),'heavy_schedule',?)",(cid,))
        return {"ok":True}

    @api.delete("/configurations/{cid}")
    def remove(cid:int):
        with store.db(True) as db:
            if db.execute("select 1 from jobs where config=? union all select 1 from remote_samples where config=? limit 1",(cid,cid)).fetchone():
                raise HTTPException(409,"history_exists_disable_instead")
            if not db.execute("delete from configs where id=?",(cid,)).rowcount:
                raise HTTPException(404,"missing")
            db.execute("insert into audit(at,action,object_id) values(unixepoch(),'delete',?)",(cid,))
        return {"ok":True}

    @api.get("/configurations/{cid}")
    def detail(cid:int):
        rows=store.rows("select "+PUBLIC+" from configs where id=?",(cid,))
        if not rows:raise HTTPException(404,"missing")
        item=rows[0]
        item["recent"]=observations(31,cid,0,100)
        item["incidents"]=store.rows("select * from incidents where config=? order by id desc limit 50",(cid,))
        return item

    @api.get("/overview")
    def overview():
        counts=store.rows("select health,count(*) n from configs where enabled group by health")
        return dict(states={r["health"]:r["n"] for r in counts},enabled=sum(r["n"] for r in counts),
                    incidents=store.rows("select count(*) n from incidents where closed is null")[0]["n"],
                    queued=store.rows("select count(*) n from jobs where state='queued'")[0]["n"])

    @api.get("/settings")
    def settings():
        return dict(live=os.getenv("LAB_LIVE")=="1",schedules=os.getenv("LAB_SCHEDULE")=="1",
            heavy=os.getenv("LAB_SPEED")=="1",telegram=os.getenv("LAB_TELEGRAM")=="1",
            daily_bytes=int(os.getenv("LAB_DAY_CAP","20000000")),per_config_bytes=int(os.getenv("LAB_CONFIG_CAP","10000000")))

    @api.post("/tests")
    def run(data:Run):
        try:
            with store.db(True) as db:
                return {"id":store.enqueue(db,data.config,data.kind)}
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from None
        except sqlite3.IntegrityError:
            raise HTTPException(409,"job_pending") from None

    @api.delete("/tests/{jid}")
    def cancel(jid:int):
        with store.db(True) as db:
            if not db.execute("update jobs set state=case when state='queued' then 'cancelled' else 'cancelling' end where id=? and state in ('queued','running','cancelling')",(jid,)).rowcount:
                raise HTTPException(409,"job_already_finished")
        return {"ok":True}

    @api.get("/tests")
    def jobs():
        return store.rows("select * from jobs order by id desc limit 100")

    @api.get("/incidents")
    def incidents():
        return store.rows("select * from incidents order by id desc limit 100")

    @api.get("/agents")
    def agents():
        return [dict(agent=store.agent,isp=os.getenv("LAB_ISP","unspecified"),heartbeat=worker.pulse)]+store.rows("select name agent,name isp,heartbeat from remote_agents order by name")

    def observations(days,cid=None,offset=0,limit=None):
        sql="""select s.id,j.config,j.agent,j.kind,s.created,s.confidence,s.baseline,s.result
            from samples s join jobs j on j.id=s.job where s.created>=?"""
        args=[time.time()-days*86400]
        if cid is not None:
            sql+=" and j.config=?"
            args.append(cid)
        rows=store.rows(sql+" order by s.id desc limit ? offset ?",(*args,limit or -1,offset))
        for row in rows:
            for key in ("baseline","result"):
                row[key]=json.loads(row[key])
        return rows

    @api.get("/measurements")
    def measurements(days:int=Query(7,ge=1,le=31),config:int|None=None,offset:int=Query(0,ge=0)):
        return observations(days,config,offset,100)

    @api.get("/summaries")
    def summaries(offset:int=Query(0,ge=0)):
        rows=store.rows("select period,start,end,totals,delivery from summaries order by end desc limit 100 offset ?",(offset,))
        for r in rows:
            r["totals"]=json.loads(r["totals"])
        return rows

    @api.get("/reports")
    def report(days:int=Query(7,ge=1,le=31),format:Literal["json","csv"]="json",config:int|None=None):
        rows=observations(days,config)
        if format=="csv":
            buf=io.StringIO()
            w=csv.writer(buf)
            w.writerow(["unix_utc","config","agent","kind","confidence","ok","http_total_ms","rx","tx"])
            for r in rows:
                v=r["result"]
                w.writerow([r["created"],r["config"],r["agent"],r["kind"],r["confidence"],v["ok"],v["total_ms"],v["rx"],v["tx"]])
            return Response(buf.getvalue(),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=measurements.csv"})
        checks=[r["result"] for r in rows if r["kind"]=="check"]
        times=sorted(v["total_ms"] for v in checks if v["ok"])
        return dict(days=days,checks=len(checks),successes=len(times),observed_success_pct=100*len(times)/len(checks) if checks else None,
                    median_ms=statistics.median(times) if times else None,p95_ms=statistics.quantiles(times,n=100,method="inclusive")[94] if len(times)>=20 else None,
                    payload_bytes=sum(r["result"]["rx"]+r["result"]["tx"] for r in rows))

    from .agents import register
    register(app,store)

    @api.get("/comparison")
    def compare(days:int=Query(7,ge=1,le=31)):
        return store.rows("""select config,agent,method,count(*) checks,round(avg(ok)*100,1) success_pct,
          round(avg(case when ok then ms end),1) mean_ms from (
            select config,agent,at,ok,ms,kind,confidence,method from remote_samples union all
            select j.config,j.agent,s.created,json_extract(result,'$.ok'),json_extract(result,'$.total_ms'),j.kind,s.confidence,json_extract(result,'$.method')
            from samples s join jobs j on j.id=s.job) where at>=? and kind='check' and confidence='valid'
            group by config,agent,method order by config,agent,method""",(time.time()-days*86400,))

    from .feeds import routes
    routes(api,store)
    app.include_router(api)
    app.mount("/",StaticFiles(directory=ROOT/"frontend",html=True))
    return app


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("command",choices=["init","serve"])
    if parser.parse_args().command=="init":
        initialize()
    else:
        import uvicorn
        uvicorn.run(create(),host="127.0.0.1",port=int(os.getenv("LAB_PORT","8765")),access_log=False)
