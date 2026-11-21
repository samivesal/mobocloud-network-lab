import json
import time


def emit(event,**fields):
    print(json.dumps({"at":int(time.time()),"event":event,**fields},separators=(",",":")),flush=True)
