#!/usr/bin/env python3
"""
vless://-ссылка из подписки/панели -> клиентский config.json для Xray (SOCKS5 на 0.0.0.0:1080).
Поддержано: VLESS + Reality поверх TCP (flow xtls-rprx-vision) и XHTTP.

  python3 tools/vless2xray.py 'vless://UUID@host:443?type=tcp&security=reality&pbk=...&sid=...&sni=...&fp=chrome&flow=xtls-rprx-vision#name' \
      -o egress/config.json
Ссылку берут из панели: Users -> нужный пользователь -> Subscription -> копировать конфиг «FIN напрямую».
"""
import argparse
import json
import sys
from urllib.parse import parse_qs, unquote, urlparse


def build(link: str, listen: str = "0.0.0.0", port: int = 1080) -> dict:
    u = urlparse(link.strip())
    if u.scheme != "vless" or not u.hostname or not u.username:
        raise ValueError("ожидается ссылка вида vless://UUID@host:port?...")
    q = {k: unquote(v[0]) for k, v in parse_qs(u.query).items()}
    net = q.get("type", "tcp")
    if net not in ("tcp", "xhttp"):
        raise ValueError(f"транспорт type={net} не поддерживается (нужен tcp или xhttp)")
    if q.get("security") != "reality":
        raise ValueError("ожидается security=reality")
    for k in ("pbk", "sni"):
        if not q.get(k):
            raise ValueError(f"в ссылке нет параметра {k}")

    user = {"id": u.username, "encryption": q.get("encryption", "none")}
    if q.get("flow"):
        user["flow"] = q["flow"]
    stream: dict = {
        "network": net,
        "security": "reality",
        "realitySettings": {
            "serverName": q["sni"],
            "fingerprint": q.get("fp", "chrome"),
            "publicKey": q["pbk"],
            "shortId": q.get("sid", ""),
            "spiderX": q.get("spx", ""),
        },
    }
    if net == "xhttp":
        xh = {"path": q.get("path", "/"), "mode": q.get("mode", "auto")}
        if q.get("host"):
            xh["host"] = q["host"]
        stream["xhttpSettings"] = xh

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "socks-in", "listen": listen, "port": port, "protocol": "socks",
            # доступ только из docker-сети bot-net, порт на хост не публикуется
            "settings": {"auth": "noauth", "udp": False},
            "sniffing": {"enabled": False},
        }],
        "outbounds": [{
            "tag": "fin", "protocol": "vless",
            "settings": {"vnext": [{"address": u.hostname, "port": u.port or 443, "users": [user]}]},
            "streamSettings": stream,
        }],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("link")
    ap.add_argument("-o", "--out", default="egress/config.json")
    a = ap.parse_args()
    try:
        cfg = build(a.link)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    o = cfg["outbounds"][0]["settings"]["vnext"][0]
    print(f"{a.out}: {o['address']}:{o['port']} ({cfg['outbounds'][0]['streamSettings']['network']}+reality)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
