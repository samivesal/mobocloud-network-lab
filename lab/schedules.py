"""Independent daily heavy schedules. Both opt-in flags are checked at dispatch."""
import os
import random
import sqlite3
import time


def setup(db):
    db.execute("""create table if not exists heavy_schedules(config integer references configs(id) on delete cascade,
      kind text,interval integer,due integer,primary key(config,kind))""")


def dispatch(store,db,now):
    if os.getenv("LAB_SPEED")!="1":
        return
    rows=db.execute("""select h.* from heavy_schedules h join configs c on c.id=h.config
      where c.enabled and h.due<=? order by h.due limit 100""",(now,)).fetchall()
    for row in rows:
        interval=row["interval"]
        try:
            store.enqueue(db,row["config"],row["kind"])
        except sqlite3.IntegrityError:
            interval=60
        except ValueError:
            interval=min(3600,interval)
        db.execute("update heavy_schedules set due=? where config=? and kind=?",
                   (now+interval+random.uniform(0,20),row["config"],row["kind"]))


def configure(db,cid,kind,interval):
    if not interval:
        db.execute("delete from heavy_schedules where config=? and kind=?",(cid,kind))
        return
    if os.getenv("LAB_SCHEDULE")!="1" or os.getenv("LAB_SPEED")!="1" or interval<3600:
        raise ValueError("heavy_schedule_disabled_or_too_frequent")
    db.execute("""insert into heavy_schedules values(?,?,?,?) on conflict(config,kind) do update
      set interval=excluded.interval,due=excluded.due""",(cid,kind,interval,time.time()+interval))


def maintenance(now, *, window=None):
    if window is None:
        window=os.getenv("LAB_PAUSE_UTC","")
    if not window:
        return False
    from datetime import datetime,timezone
    start,end=window.split("-")
    start=datetime.strptime(start,"%H:%M").time()
    end=datetime.strptime(end,"%H:%M").time()
    current=datetime.fromtimestamp(now,timezone.utc).time()
    return start<=current<end if start<end else current>=start or current<end
