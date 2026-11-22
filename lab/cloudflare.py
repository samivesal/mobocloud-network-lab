"""Bounded, repeatable measurements through an already running Xray proxy."""
from statistics import median


DOWNLOAD = "https://speed.cloudflare.com/__down?bytes="
UPLOAD = "https://speed.cloudflare.com/__up"
PING_REQUESTS = 5
PING_TIMEOUT = 30
TRANSFER_TIMEOUT = 60
CONNECT_TIMEOUT = 20


def transfer_samples(measure, port, kind, max_bytes, cancelled):
    sizes = sorted({min(250_000, max_bytes), min(1_000_000, max_bytes), max_bytes})
    attempts = []

    def sample(size):
        target = DOWNLOAD + str(size) if kind == "download" else UPLOAD
        cap = size if kind == "download" else 65536
        result = measure(target, port, kind, size, cap, cancelled=cancelled,
                         timeout=TRANSFER_TIMEOUT, connect_timeout=CONNECT_TIMEOUT)
        elapsed = result.get("transfer_ms")
        if result["ok"] and elapsed:
            result = dict(result, mbps=size * 8 / (elapsed * 1000))
        else:
            result = dict(result, ok=False, error=result.get("error") or "response", mbps=None)
        attempts.append(result)
        return result

    attempt = sample(sizes[0])
    chosen = sizes[0]
    for size in sizes[1:]:
        if not attempt["ok"] or attempt["transfer_ms"] >= 1500:
            break
        candidate = sample(size)
        if not candidate["ok"]:
            break
        chosen, attempt = size, candidate

    samples = [attempt, sample(chosen), sample(chosen)]
    successful = [result for result in samples if result["ok"]]
    if successful:
        result = dict(successful[0], mbps=median([item["mbps"] for item in successful]))
    else:
        result = dict(samples[-1])
    return dict(result, sample_ok=sum(item["ok"] for item in attempts), sample_total=len(attempts))


def ping_samples(measure, port, cancelled):
    pings = [measure(DOWNLOAD + "0", port, cancelled=cancelled,
                     timeout=PING_TIMEOUT, connect_timeout=CONNECT_TIMEOUT)
             for _ in range(PING_REQUESTS)]
    latency = [item["response_ms"] for item in pings
               if item["ok"] and item.get("response_ms") is not None]
    return dict(ping_ms=median(latency) if latency else None,
                ping_method="cloudflare_https_median",
                ping_error=None if latency else (pings[-1].get("error") or "response"),
                ping_sample_ok=len(latency), ping_sample_total=len(pings))


def benchmark(measure, port, download_bytes, upload_bytes, cancelled):
    ping = ping_samples(measure, port, cancelled)
    download = transfer_samples(measure, port, "download", download_bytes, cancelled)
    upload = transfer_samples(measure, port, "upload", upload_bytes, cancelled)
    return dict(download, **ping, upload=upload)
