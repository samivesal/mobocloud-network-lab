"""Validate operator policy once before the background worker starts."""
import os
import time
from urllib.parse import urlsplit
from .schedules import maintenance
from .store import amount


def validate(environ=None):
    env = os.environ if environ is None else environ
    for name in ("LIVE","SCHEDULE","SPEED","TELEGRAM","HTTPS","SEND_SUMMARIES"):
        if env.get("LAB_"+name,"0") not in ("0","1"):
            raise ValueError("Invalid flag LAB_"+name)
    for name,minimum,default in [("CONFIRM",1,3),("RECOVER",1,2),("RETENTION_DAYS",0,0),
                                  ("BASELINE_MAX_MS",0,0),("CONFIG_CAP",0,10000000),("DAY_CAP",0,20000000)]:
        if int(env.get("LAB_"+name,str(default)))<minimum:
            raise ValueError("Invalid policy LAB_"+name)
    maintenance(time.time(),window=env.get("LAB_PAUSE_UTC",""))
    amount("upload",env)
    amount("download",env)
    for key in ("CHECK_URL","UPLOAD_URL","DOWNLOAD_URL","IP_URL","IP_FALLBACK_URL"):
        raw=env.get("LAB_"+key)
        if raw:
            u=urlsplit(raw)
            if u.scheme!="https" or not u.hostname or u.username or u.password or u.fragment:
                raise ValueError("Invalid HTTPS target LAB_"+key)
    if env.get("LAB_LIVE")=="1" and not env.get("LAB_CHECK_URL"):
        raise ValueError("Set LAB_CHECK_URL before enabling live jobs")
    if env.get("LAB_TELEGRAM")=="1" and not all(env.get(k) for k in ("LAB_BOT_TOKEN","LAB_CHAT_ID")):
        raise ValueError("Telegram requires server-side credentials")
