"""Linux exec wrapper: terminate this specific engine when its parent dies."""
import ctypes
import os
import signal
import sys

if __name__=="__main__":
    parent=int(sys.argv[1])
    if ctypes.CDLL(None,use_errno=True).prctl(1,signal.SIGKILL,0,0,0)!=0 or os.getppid()!=parent:
        sys.exit(1)
    os.execv(sys.argv[2],sys.argv[2:])
