"""UTC calendar reports, finalized after each period closes."""
from datetime import datetime, timedelta, timezone
import json
import os
import time


def setup(db):
    db.executescript("""create table if not exists summaries(id integer primary key,period text,start integer,end integer,
      totals text,delivery text,due integer default 0,attempts integer default 0,unique(period,start));
      create table if not exists report_clock(id integer primary key check(id=1),observed integer);
      insert or ignore into report_clock values(1,unixepoch());""")


def prior_periods(now):
    day=datetime.fromtimestamp(now,timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    monday=day-timedelta(days=day.weekday())
    first=day.replace(day=1)
    for name,start,end in [("daily",day-timedelta(days=1),day),("weekly",monday-timedelta(days=7),monday),
                           ("monthly",(first-timedelta(days=1)).replace(day=1),first)]:
        yield name,int(start.timestamp()),int(end.timestamp())


def finalize(store,now):
    with store.db(True) as db:
        for period,start,end in prior_periods(now):
            if db.execute("select 1 from summaries where period=? and start=?",(period,start)).fetchone():
                continue
            stats=dict(db.execute("""select count(*) checks,
                coalesce(sum(json_extract(result,'$.ok')),0) successes,
                sum(case when confidence='degraded_baseline' then 1 else 0 end) degraded_baselines
                from samples s join jobs j on j.id=s.job where kind='check' and s.created>=? and s.created<?""",(start,end)).fetchone())
            if not stats["checks"] and end<=db.execute("select observed from report_clock").fetchone()[0]:
                continue
            stats["incidents"]=db.execute("select count(*) from incidents where opened>=? and opened<?",(start,end)).fetchone()[0]
            delivery="pending" if os.getenv("LAB_TELEGRAM")=="1" and os.getenv("LAB_SEND_SUMMARIES")=="1" else "off"
            db.execute("insert into summaries(period,start,end,totals,delivery) values(?,?,?,?,?)",
                       (period,start,end,json.dumps(stats),delivery))
        days=int(os.getenv("LAB_RETENTION_DAYS","0"))
        if days>0:
            cutoff=now-max(1,days)*86400
            # Keep inventory, jobs, incidents, audit and finalized summaries.
            db.execute("delete from samples where created<?",(cutoff,))
            db.execute("delete from remote_samples where at<?",(cutoff,))
