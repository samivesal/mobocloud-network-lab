"""A saved feed is refreshed at the start of every download-test round."""
import os
import sqlite3
import time
from urllib.parse import unquote,urlsplit
from fastapi import HTTPException
from pydantic import BaseModel,Field
from typing import Literal
from .config import decode,parse_document
from .schedules import maintenance
from .store import amount
from .subscription import download,validate_url
from .uplink import interfaces,require_link
from .runner import Failure


ACTIVE_JOBS = "('queued','running','cancelling')"
ACTIVE_ROUND = f"""r.state in ('pending','loading') or exists(
    select 1 from feed_jobs f join jobs j on j.id=f.job
    where f.round=r.id and j.state in {ACTIVE_JOBS})"""


def setup(db):
    db.executescript("""
    create table if not exists feeds(id integer primary key,name text not null,url blob not null,
      iface text not null,hours integer not null,enabled integer not null,due real not null,
      seen integer default 0,queued integer default 0,rejected integer default 0,skipped integer default 0,error text);
    create table if not exists feed_jobs(job integer primary key references jobs(id),feed integer references feeds(id),
      iface text not null,label text not null,round integer references feed_rounds(id));
    create table if not exists feed_rounds(id integer primary key autoincrement,
      feed integer not null references feeds(id),iface text not null,created real not null,
      kind text not null,state text not null,seen integer default 0,
      rejected integer default 0,skipped integer default 0,error text);
    create table if not exists feed_skips(round integer not null references feed_rounds(id) on delete cascade,
      label text not null,error text not null);
    create index if not exists feed_skips_round on feed_skips(round);
    """)
    db.execute("begin immediate")
    if "cloudflare_upload" not in {column[1] for column in db.execute("pragma table_info(feed_rounds)")}:
        db.execute("alter table feed_rounds add column cloudflare_upload integer not null default 0")
    if "subscription_test" not in {column[1] for column in db.execute("pragma table_info(jobs)")}:
        db.execute("alter table jobs add column subscription_test integer not null default 0")
        db.execute("update jobs set subscription_test=1 where id in(select job from feed_jobs)")
    feed_columns = {column[1] for column in db.execute("pragma table_info(feeds)")}
    if "deleted" not in feed_columns:
        db.execute("alter table feeds add column deleted integer not null default 0")
    if "round" not in {column[1] for column in db.execute("pragma table_info(feed_jobs)")}:
        db.execute("alter table feed_jobs add column round integer references feed_rounds(id)")
    db.execute("create index if not exists feed_jobs_round on feed_jobs(round)")
    db.execute("create index if not exists feed_rounds_feed on feed_rounds(feed,id)")
    # Earlier releases saved jobs in order, but did not record round IDs.
    previous = None
    for row in db.execute("""select f.job,f.feed,f.iface,j.created from feed_jobs f
        join jobs j on j.id=f.job where f.round is null order by j.id""").fetchall():
        job, feed, iface, created = row
        if (previous is None or previous[:3] != (job - 1, feed, iface)
                or created - previous[3] not in (0, 1, 2)):
            round_id = db.execute("""insert into feed_rounds(feed,iface,created,kind,state)
                values(?,?,?,'legacy','ready')""",(feed,iface,created)).lastrowid
        db.execute("update feed_jobs set round=? where job=?",(round_id,job))
        db.execute("update feed_rounds set seen=seen+1 where id=?",(round_id,))
        previous = (job,feed,iface,created)


def ready():
    return all(os.getenv(k)=="1" for k in ("LAB_LIVE","LAB_SPEED","LAB_SCHEDULE")) and bool(os.getenv("LAB_DOWNLOAD_URL") and os.getenv("LAB_CHECK_URL"))


class Selection(BaseModel):
    iface:str=Field(min_length=1,max_length=15)
    hours:Literal[12,24]


class Plan(Selection):
    name:str=Field(default="",max_length=80)
    url:str=Field(max_length=4096)


class Period(BaseModel):
    hours:Literal[12,24]


def active_round(db,feed):
    return db.execute(f"select id from feed_rounds r where feed=? and ({ACTIVE_ROUND})",
                      (feed,)).fetchone()


def history_filter(fid):
    return (f"not ({ACTIVE_ROUND}) and (? is null or r.feed=?)", (fid,fid))


def history_count(db,where,args):
    return db.execute(f"""select coalesce(sum(
        (select count(*) from feed_jobs where round=r.id)
        + (select count(*) from feed_skips where round=r.id)),0)
        from feed_rounds r where {where}""",args).fetchone()[0]


def stop_rounds(db,fid):
    db.execute("""update feed_rounds set state='ready',error='cancelled'
        where feed=? and state in ('pending','loading')""",(fid,))
    db.execute("""update jobs set state=case when state='queued' then 'cancelled' else 'cancelling' end
        where state in ('queued','running') and id in(select job from feed_jobs where feed=?)""",(fid,))


def routes(api,store):
    @api.get("/subscriptions")
    def list_feeds(summary:bool=False):
        data = dict(interfaces=interfaces(),ready=ready(),bytes=amount("download"),
            upload_ready=bool(os.getenv("LAB_UPLOAD_URL")),
            feeds=store.rows(f"""select id,name,iface,hours,enabled,due,seen,queued,rejected,skipped,error,
                exists(select 1 from feed_rounds r where r.feed=feeds.id and ({ACTIVE_ROUND})) busy
                from feeds where not deleted order by id desc"""))
        if summary:
            progress = {row["feed"]:row for row in store.rows(f"""
                select r.feed,count(j.id) total,coalesce(sum(j.state not in {ACTIVE_JOBS}),0) done
                from feed_rounds r left join feed_jobs f on f.round=r.id
                left join jobs j on j.id=f.job where {ACTIVE_ROUND} group by r.id""")}
            for feed in data["feeds"]:
                feed["progress"] = progress.get(feed["id"])
        else:
            data["results"]=store.rows("""select f.name,r.label,r.iface,j.created,j.state,
                coalesce(j.error,json_extract(s.result,'$.error')) error,json_extract(s.result,'$.mbps') mbps,
                json_extract(s.result,'$.ping_ms') ping_ms,json_extract(s.result,'$.ping_method') ping_method,
                json_extract(s.result,'$.ping_error') ping_error,
                json_extract(s.result,'$.upload.mbps') upload_mbps,json_extract(s.result,'$.upload.error') upload_error
                from feed_jobs r join jobs j on j.id=r.job join feeds f on f.id=r.feed left join samples s on s.job=j.id
                order by j.id desc limit 200""")
        return data

    @api.get("/subscriptions/rounds")
    def rounds(before:int|None=None):
        with store.db() as db:
            db.execute("begin")
            batches = [dict(row, results=[]) for row in db.execute("""
                select r.id,r.feed,r.iface,r.created,r.kind,r.state,r.seen,r.rejected,r.skipped,r.error,f.name from feed_rounds r join feeds f on f.id=r.feed
                where (? is null or r.id<?) order by r.id desc limit 11""",(before,before))]
            more = len(batches)>10
            batches = batches[:10]
            by_id = {row["id"]:row for row in batches}
            if by_id:
                for row in db.execute("""
                    select f.round,f.label,j.state,coalesce(s.created,j.created) at,
                    coalesce(j.error,json_extract(s.result,'$.error')) error,
                    json_extract(s.result,'$.ok') ok,json_extract(s.result,'$.mbps') mbps,
                    json_extract(s.result,'$.ping_ms') ping_ms,
                    json_extract(s.result,'$.ping_method') ping_method,
                    json_extract(s.result,'$.ping_error') ping_error,
                    json_extract(s.result,'$.upload.ok') upload_ok,
                    json_extract(s.result,'$.upload.mbps') upload_mbps,
                    json_extract(s.result,'$.upload.error') upload_error
                    from feed_jobs f join jobs j on j.id=f.job left join samples s on s.job=j.id
                    where f.round between ? and ? order by j.id""",(min(by_id),max(by_id))):
                    by_id[row["round"]]["results"].append(dict(row))
                for row in db.execute("""
                    select f.round,f.label,'skipped' state,r.created at,f.error,null ok,null mbps
                    from feed_skips f join feed_rounds r on r.id=f.round
                    where f.round between ? and ? order by f.rowid""",(min(by_id),max(by_id))):
                    by_id[row["round"]]["results"].append(dict(row))
        return {"rounds":batches,"before":batches[-1]["id"] if more else None}

    @api.get("/subscriptions/history")
    def history(fid:int|None=None):
        where,args=history_filter(fid)
        with store.db() as db:
            count=history_count(db,where,args)
        return {"count":count}

    @api.delete("/subscriptions/history")
    def clear_history(count:int,fid:int|None=None):
        where,args=history_filter(fid)
        with store.db(True) as db:
            select_jobs=f"select f.job from feed_jobs f join feed_rounds r on r.id=f.round where {where}"
            current=history_count(db,where,args)
            if current!=count:
                raise HTTPException(409,"history_changed")
            db.execute(f"delete from samples where job in ({select_jobs})",args)
            db.execute(f"delete from feed_jobs where job in ({select_jobs})",args)
            db.execute(f"delete from feed_rounds as r where {where}",args)
        return {"deleted":current}

    @api.post("/subscriptions")
    def save(plan:Plan):
        try:
            validate_url(plan.url)
            require_link(plan.iface)
        except (ValueError,Failure):
            raise HTTPException(422,"invalid_url_or_inactive_interface") from None
        if not ready():
            raise HTTPException(409,"subscription_speed_policy_disabled")
        due=time.time()+3600*plan.hours
        with store.db(True) as db:
            id_=db.execute("""insert into feeds(name,url,iface,hours,enabled,due)
                values(?,?,?,?,1,?)""",(plan.name.strip() or "اشتراک",store.cipher.encrypt(plan.url.encode()),
                plan.iface,plan.hours,due)).lastrowid
            if not plan.name.strip():
                db.execute("update feeds set name=? where id=?",(f"اشتراک {id_}",id_))
        return {"id":id_,"due":due}

    @api.post("/subscriptions/{fid}/run")
    def run_now(fid:int,cloudflare_upload:bool=False):
        if not ready():
            raise HTTPException(409,"subscription_speed_policy_disabled")
        with store.db(True) as db:
            feed=db.execute("select iface from feeds where id=? and not deleted",(fid,)).fetchone()
            if not feed:
                raise HTTPException(404,"missing")
            current=active_round(db,fid)
            if current:
                return {"id":current["id"],"queued":False}
            try:
                require_link(feed["iface"])
            except Failure:
                raise HTTPException(422,"interface_unavailable") from None
            rid=db.execute("""insert into feed_rounds(feed,iface,created,kind,state,cloudflare_upload)
                values(?,?,?,'manual','pending',?)""",(fid,feed["iface"],time.time(),cloudflare_upload)).lastrowid
        return {"id":rid,"queued":True}

    @api.delete("/subscriptions/{fid}")
    def delete_feed(fid:int):
        with store.db(True) as db:
            if not db.execute("update feeds set deleted=1,enabled=0,url=x'' where id=?",(fid,)).rowcount:
                raise HTTPException(404,"missing")
            stop_rounds(db,fid)
        return {"ok":True}

    @api.put("/subscriptions/{fid}/period")
    def change_period(fid:int,period:Period):
        with store.db(True) as db:
            if not db.execute("""update feeds set hours=?,due=? where id=? and not deleted and hours!=?""",
                              (period.hours,time.time()+3600*period.hours,fid,period.hours)).rowcount:
                if not db.execute("select 1 from feeds where id=? and not deleted",(fid,)).fetchone():
                    raise HTTPException(404,"missing")
        return {"ok":True}

    @api.put("/subscriptions/{fid}")
    def change(fid:int,selection:Selection):
        try:
            require_link(selection.iface)
        except Failure:
            raise HTTPException(422,"interface_unavailable") from None
        with store.db(True) as db:
            if not db.execute("update feeds set iface=?,hours=?,due=? where id=? and not deleted",
                    (selection.iface,selection.hours,time.time(),fid)).rowcount:
                raise HTTPException(404,"missing")
            db.execute("""update jobs set state=case when state='queued' then 'cancelled' else 'cancelling' end
                where state in ('queued','running') and id in(select job from feed_jobs where feed=?)""",(fid,))
        return {"ok":True}

    @api.patch("/subscriptions/{fid}")
    def toggle(fid:int,enabled:bool):
        if enabled and not ready():
            raise HTTPException(409,"subscription_speed_policy_disabled")
        with store.db(True) as db:
            current=db.execute("select enabled,hours from feeds where id=? and not deleted",(fid,)).fetchone()
            if not current:
                raise HTTPException(404,"missing")
            if current["enabled"]==enabled and enabled:
                return {"ok":True}
            if enabled:
                db.execute("update feeds set enabled=1,due=? where id=?",
                           (time.time()+3600*current["hours"],fid))
            else:
                db.execute("update feeds set enabled=0 where id=?",(fid,))
                stop_rounds(db,fid)
        return {"ok":True}


def entries(body):
    if body.lstrip().startswith("{"):
        return [("1",parse_document(body)[0])],0
    if "://" not in body:
        body=decode("".join(body.split()))
    lines=[line.strip() for line in body.splitlines() if line.strip()]
    if not lines or len(lines)>1000:
        raise ValueError("invalid_feed_size")
    accepted=[]
    rejected=0
    for i,line in enumerate(lines,1):
        try:
            out=parse_document(line)[0]
            label=unquote(urlsplit(line).fragment)[:100] or str(i)
            accepted.append((label,out))
        except (ValueError,TypeError,KeyError,IndexError,AttributeError):
            rejected+=1
    return accepted,rejected


def refresh(store):
    now=time.time()
    if not ready() or maintenance(now):
        return
    with store.db(True) as db:
        request=db.execute("""select r.id round_id,r.iface,f.url from feed_rounds r
            join feeds f on f.id=r.feed where r.state='pending' order by r.id limit 1""").fetchone()
        if request:
            rid,iface,url=request
            db.execute("update feed_rounds set state='loading' where id=?",(rid,))
        else:
            feed=db.execute(f"""select * from feeds f where not deleted and enabled and due<=? and not exists(
                select 1 from feed_rounds r where r.feed=f.id and ({ACTIVE_ROUND}))
                order by due,id limit 1""",(now,)).fetchone()
            if not feed:
                return
            iface,url=feed["iface"],feed["url"]
            rid=db.execute("""insert into feed_rounds(feed,iface,created,kind,state)
                values(?,?,?,'scheduled','loading')""",(feed["id"],iface,now)).lastrowid
            db.execute("update feeds set due=? where id=?",(now+3600*feed["hours"],feed["id"]))
    try:
        require_link(iface)
        accepted,rejected=entries(download(store.cipher.decrypt(url).decode(),iface))
    except Exception as exc:
        code="interface_unavailable" if isinstance(exc,Failure) else "feed_refresh_failed"
        with store.db(True) as db:
            db.execute("update feed_rounds set state='ready',error=? where id=? and state='loading'",(code,rid))
            if not request:
                db.execute("""update feeds set error=?,due=? where id=? and enabled and due=?
                    and exists(select 1 from feed_rounds where id=? and error=?)""",
                    (code,now+300,feed["id"],now+3600*feed["hours"],rid,code))
        return
    with store.db(True) as db:
        batch=db.execute("select feed,state from feed_rounds where id=?",(rid,)).fetchone()
        if not batch or batch["state"]!="loading":
            return
        fid=batch["feed"]
        ids=set()
        queued=skipped=0
        reasons=set()
        for label,out in accepted:
            cid=store.add(db,out,label,"Other","")
            if cid in ids:
                continue
            ids.add(cid)
            try:
                jid=store.enqueue(db,cid,"download",subscription_test=True)
            except (ValueError,sqlite3.IntegrityError) as exc:
                skipped+=1
                code=str(exc) if isinstance(exc,ValueError) else "job_pending"
                reasons.add(code)
                db.execute("insert into feed_skips(round,label,error) values(?,?,?)",(rid,label,code))
                continue
            db.execute("insert into feed_jobs(job,feed,iface,label,round) values(?,?,?,?,?)",(jid,fid,iface,label,rid))
            queued+=1
        seen=len(accepted)+rejected
        error=",".join(sorted(reasons)) or None
        db.execute("""update feed_rounds set state='ready',seen=?,rejected=?,skipped=?,error=?
            where id=?""",(seen,rejected,skipped,error,rid))
        db.execute("update feeds set seen=?,queued=?,rejected=?,skipped=?,error=? where id=?",
                   (seen,queued,rejected,skipped,error,fid))
