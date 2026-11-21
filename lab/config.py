"""Translate supported share formats to one constrained outbound."""
import base64
import ipaddress
import json
import re
import uuid
from urllib.parse import parse_qsl, unquote, urlsplit


def decode(value):
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode()


def parse(raw):
    u = urlsplit(raw.strip())
    if u.scheme == "ss" and "@" not in u.netloc:
        u = urlsplit("ss://" + decode(u.netloc))
    if not u.hostname or not u.port or not u.username or u.path not in ("", "/"):
        raise ValueError("invalid endpoint")
    host = u.hostname.lower()
    if not re.fullmatch(r"[a-z0-9.:-]+", host):
        raise ValueError("invalid host")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr and not addr.is_global:
        raise ValueError("nonpublic endpoint")
    pairs = parse_qsl(u.query, keep_blank_values=True, strict_parsing=True)
    q = dict(pairs)
    if len(q) != len(pairs):
        raise ValueError("duplicate option")
    if u.scheme == "ss":
        if q:
            raise ValueError("unsupported shadowsocks plugin or option")
        auth = unquote(u.username)
        method, password = ((auth, unquote(u.password)) if u.password is not None
                            else decode(auth).split(":", 1))
        if method not in {"aes-128-gcm", "aes-256-gcm", "chacha20-ietf-poly1305",
                          "2022-blake3-aes-256-gcm"} or not password:
            raise ValueError("unsupported cipher or missing password")
        return {"protocol":"shadowsocks","settings":{"servers":[dict(address=host, port=u.port, method=method, password=password)]}}
    if u.scheme != "vless" or u.password is not None:
        raise ValueError("unsupported protocol")
    uid = str(uuid.UUID(u.username))
    network = q.pop("type", "tcp")
    header_type = q.pop("headerType", "none")
    security = q.pop("security", "none")
    user = dict(id=uid, encryption=q.pop("encryption","none"))
    if "flow" in q:
        user["flow"] = q.pop("flow")
    stream = dict(network=network, security=security)
    if security in ("tls","reality"):
        fields = {"sni":"serverName","fp":"fingerprint"}
        fields.update({"pbk":"publicKey","sid":"shortId","spx":"spiderX"} if security=="reality" else {})
        opts = {dest:q.pop(src) for src,dest in fields.items() if src in q}
        if security=="tls" and "alpn" in q:
            opts["alpn"] = q.pop("alpn").split(",")
        elif security == "reality":
            # Xray's REALITY client uses the fingerprint's ClientHello.
            q.pop("alpn", None)
        stream[security+"Settings"] = opts
    elif security != "none":
        raise ValueError("unsupported security")
    if header_type == "http" and network in ("tcp", "raw"):
        request = {}
        if "host" in q:
            request["headers"] = {"Host": q.pop("host").split(",")}
        if "path" in q:
            request["path"] = q.pop("path").split(",")
        stream[network+"Settings"] = {"header": {"type": "http", "request": request}}
    elif header_type != "none":
        raise ValueError("unsupported header type")
    if network in ("ws","xhttp","grpc"):
        fields = {"grpc":["serviceName","authority"],"ws":["path","host"],"xhttp":["mode","host","path"]}[network]
        stream[network+"Settings"] = {k:q.pop(k) for k in fields if k in q}
    elif network not in ("tcp","raw"):
        raise ValueError("unsupported transport")
    if q:
        raise ValueError("unsupported option")
    return dict(protocol="vless", settings={"vnext":[dict(address=host,port=u.port,users=[user])]},streamSettings=stream)


def peer(out):
    return next(iter(out["settings"].values()))[0]


def parse_document(text):
    """Never execute an imported Xray document; rebuild the permitted outbound."""
    from urllib.parse import quote, urlencode
    if not text.lstrip().startswith("{"):
        lines=[s.strip() for s in text.splitlines() if s.strip()]
        if len(lines)==1 and "://" not in lines[0]:
            lines=decode(lines[0]).splitlines()
        if not 1<=len(lines)<=100:
            raise ValueError("invalid_list")
        return [parse(line) for line in lines]
    doc=json.loads(text)
    entries=doc.get("outbounds",[doc])
    if not isinstance(entries,list) or len(entries)!=1:
        raise ValueError("expected one outbound")
    out=entries[0]
    protocol=out["protocol"]
    dst=peer(out)
    host=dst["address"]
    host="["+host+"]" if ":" in host else host
    dest=host+":"+str(dst["port"])
    if protocol=="shadowsocks":
        auth=quote(dst["method"],safe="")+":"+quote(dst["password"],safe="")
        rebuilt=parse("ss://"+auth+"@"+dest)
    elif protocol=="vless":
        user=dst["users"][0]
        stream=out.get("streamSettings",{})
        net=stream.get("network","tcp")
        sec=stream.get("security","none")
        q=dict(type=net,security=sec,encryption=user.get("encryption","none"))
        if "flow" in user:
            q["flow"]=user["flow"]
        names={"serverName":"sni","fingerprint":"fp","publicKey":"pbk","shortId":"sid","spiderX":"spx"}
        for k,v in stream.get(sec+"Settings",{}).items():
            q[names.get(k,k)]=",".join(v) if k=="alpn" else v
        transport = stream.get(net+"Settings",{})
        if net in ("tcp", "raw") and "header" in transport:
            header = transport["header"]
            request = header.get("request", {})
            q["headerType"] = header["type"]
            if "Host" in request.get("headers", {}):
                q["host"] = ",".join(request["headers"]["Host"])
            if "path" in request:
                q["path"] = ",".join(request["path"])
        else:
            q.update(transport)
        rebuilt=parse("vless://"+user["id"]+"@"+dest+"?"+urlencode(q))
    else:
        raise ValueError("unsupported outbound")
    expected=dict(out)
    expected.pop("tag",None)
    if protocol=="vless":
        expected.setdefault("streamSettings",{})
        expected["streamSettings"].setdefault("network","tcp")
        expected["streamSettings"].setdefault("security","none")
        user.setdefault("encryption","none")
    if rebuilt!=expected:
        raise ValueError("unsupported outbound settings")
    return [rebuilt]
