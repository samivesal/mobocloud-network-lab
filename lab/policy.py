"""Validate operator policy once before the background worker starts."""
import os
import time
from urllib.parse import urlsplit
from .schedules import maintenance
from .store import amount


def validate():
    for name in ("LIVE","SCHEDULE","SPEED","TELEGRAM","HTTPS","SEND_SUMMARIES"):
        if os.getenv("LAB_"+name,"0") not in ("0","1"):
            raise ValueError("Invalid flag LAB_"+name)
    for name,minimum,default in [("CONFIRM",1,3),("RECOVER",1,2),("RETENTION_DAYS",0,0),
                                  ("BASELINE_MAX_MS",0,0),("CONFIG_CAP",0,10000000),("DAY_CAP",0,20000000)]:
        if int(os.getenv("LAB_"+name,str(default)))<minimum:
            raise ValueError("Invalid policy LAB_"+name)
    maintenance(time.time())
    amount("upload")
    amount("download")
    for key in ("CHECK_URL","UPLOAD_URL","DOWNLOAD_URL","IP_URL","IP_FALLBACK_URL"):
        raw=os.getenv("LAB_"+key)
        if raw:
            u=urlsplit(raw)
            if u.scheme!="https" or not u.hostname or u.username or u.password or u.fragment:
                raise ValueError("Invalid HTTPS target LAB_"+key)
    if os.getenv("LAB_LIVE")=="1" and not os.getenv("LAB_CHECK_URL"):
        raise ValueError("Set LAB_CHECK_URL before enabling live jobs")
    if os.getenv("LAB_TELEGRAM")=="1" and not all(os.getenv(k) for k in ("LAB_BOT_TOKEN","LAB_CHAT_ID")):
        raise ValueError("Telegram requires server-side credentials")
