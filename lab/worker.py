import hashlib
import json
import os
import random
import sqlite3
import threading
import time

import httpx
from . import cloudflare
from .events import emit
from .runner import Failure, pinned, probe, proxy
from .store import amount, speed_reservation


def subscription_probe(*args, **kwargs):
    try:
        return probe(*args, **kwargs)
    except Failure as exc:
        if str(exc)=="cancelled":
            raise
        return dict(ok=False,error=str(exc),mbps=None)


class Worker:
    def __init__(self, store):
        self.store=store
        self.stop=threading.Event()
        self.pulse=0
        self.last_report=0

    def claim(self):
        with self.store.db(True) as db:
            now=time.time()
            from .schedules import maintenance
            if maintenance(now):
                return None
            if os.getenv("LAB_LIVE")=="1" and os.getenv("LAB_SCHEDULE")=="1":
                from .schedules import dispatch
                dispatch(self.store,db,now)
                for cfg in db.execute("select id,interval from configs where enabled and interval>0 and due<=?",(now,)).fetchall():
                    try:
                        self.store.enqueue(db,cfg["id"],"check")
                    except sqlite3.IntegrityError:
                        pass
                    db.execute("update configs set due=? where id=?",(now+cfg["interval"]+random.uniform(0,10),cfg["id"]))
            job=db.execute("select * from jobs where state='queued' order by id limit 1").fetchone()
            if job:
                db.execute("update jobs set state='running' where id=?",(job["id"],))
                return dict(job)

    def test(self,job):
        store=self.store
        cid=job["config"]
        cfg=store.rows("select secret,enabled from configs where id=?",(cid,))[0]
        if not cfg["enabled"] or os.getenv("LAB_LIVE")!="1":
            raise Failure("disabled")
        out=json.loads(store.cipher.decrypt(cfg["secret"]))
        from .uplink import require_link,tunnel
        from .config import peer
        batch=store.rows("""select f.iface,r.cloudflare_upload from feed_jobs f
            left join feed_rounds r on r.id=f.round where f.job=?""",(job["id"],))
        iface=batch[0]["iface"] if batch else None
        if iface:
            require_link(iface)
        approved=[peer(out)["address"]] if batch else os.getenv("LAB_PEERS","").lower().split(",")
        out=pinned(out,approved)
        kind=job["kind"]
        url=os.environ["LAB_CHECK_URL"]
        ping_only=kind=="ping"
        speed=kind not in ("check","ping")
        subscription=job.get("subscription_test")
        if speed and not subscription and job["created"]//86400!=int(time.time())//86400:
            raise Failure("reservation_expired")
        if speed and os.getenv("LAB_SPEED")!="1":
            raise Failure("speed_disabled")
        size=amount(kind) if speed else 0
        if size and not subscription and speed_reservation(size)>job["reserved"]:
            raise Failure("budget_changed")
        upload_url=os.getenv("LAB_UPLOAD_URL")
        cloudflare_test=ping_only or (subscription and batch and batch[0]["cloudflare_upload"])
        if cloudflare_test:
            url=cloudflare.DOWNLOAD+"0"
        from .baseline import interface_stats
        def cancelled():
            return self.stop.is_set() or store.rows("select state from jobs where id=?",(job["id"],))[0]["state"]=="cancelling"
        measure=subscription_probe if subscription else probe
        if cloudflare_test:
            base=measure(url,cancelled=cancelled,iface=iface,timeout=cloudflare.PING_TIMEOUT,
                         connect_timeout=cloudflare.CONNECT_TIMEOUT)
        else:
            base=measure(url,cancelled=cancelled,iface=iface)
        base["interface"]=interface_stats(iface)
        relay_options={"timeout":cloudflare.CONNECT_TIMEOUT} if cloudflare_test else {}
        with tunnel(out,iface,**relay_options) as bound, proxy(bound,os.getenv("LAB_XRAY","xray"),cancelled) as port:
            if ping_only:
                ping=cloudflare.ping_samples(measure,port,cancelled)
                result=dict(ok=ping["ping_ms"] is not None,error=ping["ping_error"],
                            total_ms=None,rx=0,tx=0,**ping)
            elif cloudflare_test:
                result=cloudflare.benchmark(measure,port,size,amount("upload"),cancelled)
            elif subscription:
                target=os.environ["LAB_DOWNLOAD_URL"]
                ping=measure(url,port,cancelled=cancelled)
                result=measure(target,port,"download",size,size,cancelled=cancelled)
                result["ping_ms"]=ping.get("response_ms") if ping["ok"] else None
                result["ping_method"]="https_response"
                result["ping_error"]=ping.get("error")
                result["upload"]=(measure(upload_url,port,"upload",amount("upload"),65536,cancelled=cancelled) if upload_url
                                  else dict(ok=False,error="subscription_upload_unavailable",mbps=None))
            else:
                target=url if not size else os.environ["LAB_"+kind.upper()+"_URL"]
                result=probe(target,port,kind,size,size if kind=="download" else 65536,cancelled=cancelled)
            method="cloudflare-v3:" if cloudflare_test else "curl-v1:"
            result["method"]=hashlib.sha256((method+url).encode()).hexdigest()[:16]
            if result["ok"] and not ping_only and os.getenv("LAB_IP_URL"):
                for target in filter(None,[os.getenv("LAB_IP_URL"),os.getenv("LAB_IP_FALLBACK_URL")]):
                    ip=measure(target,port,cap=128,body=True,cancelled=cancelled)
                    result["exit_ip"]=ip.get("exit_ip")
                    if result["exit_ip"]:
                        break
        with store.db(True) as db:
            if db.execute("select state from jobs where id=?",(job["id"],)).fetchone()[0]=="cancelling":
                raise Failure("cancelled")
            store.record(db,job,base,result)
            db.execute("update jobs set state='done' where id=?",(job["id"],))

    def notify(self):
        if os.getenv("LAB_TELEGRAM")!="1" or self.store.rows("select 1 from telegram_link where id=1"):
            return
        rows=self.store.rows("""select a.*,i.config,c.product from alerts a join incidents i on i.id=a.incident
             join configs c on c.id=i.config where a.state='pending' and a.due<=? order by a.id limit 1""",(time.time(),))
        summary=False
        if rows:
            a=rows[0]
            message=f"Mobocloud {a['event']} · {a['product']} #{a['config']} · {self.store.agent} · incident {a['incident']}"
        elif os.getenv("LAB_SEND_SUMMARIES")=="1":
            rows=self.store.rows("select * from summaries where delivery='pending' and due<=? order by id limit 1",(time.time(),))
            if not rows:
                return
            a=rows[0]
            stats=json.loads(a["totals"])
            summary=True
            message=f"Mobocloud {a['period']} · {self.store.agent} · UTC {a['start']}–{a['end']} · successful checks {stats['successes']}/{stats['checks']}"
        else:
            return
        try:
            with httpx.Client(timeout=5,trust_env=False) as client:
                r=client.post("https://api.telegram.org/bot"+os.environ["LAB_BOT_TOKEN"]+"/sendMessage",
                    json={"chat_id":os.environ["LAB_CHAT_ID"],"text":message})
                ok=r.is_success and r.json().get("ok")
        except (httpx.HTTPError,ValueError,KeyError):
            ok=False
        with self.store.db(True) as db:
            table_column="summaries set delivery" if summary else "alerts set state"
            db.execute("update "+table_column+"=?,attempts=attempts+1,due=? where id=?",
                ("sent" if ok else "pending",time.time()+min(3600,30*2**min(7,a["attempts"])),a["id"]))

    def loop(self):
        with self.store.db(True) as db:
            db.execute("update jobs set state='interrupted' where state in ('running','cancelling')")
            db.execute("update feed_rounds set state='pending' where state='loading'")
        while not self.stop.is_set():
            self.pulse=time.time()
            if self.pulse-self.last_report>=3600:
                from .reports import finalize
                finalize(self.store,self.pulse)
                self.last_report=self.pulse
            from .feeds import refresh
            refresh(self.store)
            job=self.claim()
            if job:
                started=time.monotonic()
                fields=dict(job_id=job["id"],config_id=job["config"],agent=self.store.agent)
                emit("job_started",**fields)
                try:
                    self.test(job)
                    emit("job_completed",duration_ms=int((time.monotonic()-started)*1000),**fields)
                except Exception as exc:
                    code=str(exc) if isinstance(exc,Failure) else "runner_error"
                    emit("job_failed",severity="warning",error=code,**fields)
                    with self.store.db(True) as db:
                        db.execute("update jobs set state=?,error=? where id=?",('cancelled' if code=='cancelled' else 'error',code,job["id"]))
            self.notify()
            self.stop.wait(1)
