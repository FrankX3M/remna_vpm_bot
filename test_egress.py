"""Проверки без Telegram и без панели: python test_egress.py
1) конвертер vless:// -> config.json Xray; 2) бот реально идёт через SOCKS5 и отдаёт ИМЯ хоста прокси
(а не резолвит его сам: DNS в РФ для api.telegram.org подменяется/блокируется)."""
import asyncio
import os
import sys

os.environ.update(BOT_TOKEN="1:test", REMNAWAVE_URL="http://x", REMNAWAVE_TOKEN="t")
sys.path.insert(0, "tools")
import vless2xray  # noqa: E402

LINK = ("vless://11111111-2222-3333-4444-555555555555@fin.example.com:8443?type=tcp&security=reality"
        "&pbk=PUBKEY&sid=ab12&sni=www.microsoft.com&fp=chrome&flow=xtls-rprx-vision&spx=%2F#FIN")
c = vless2xray.build(LINK)
o = c["outbounds"][0]
assert o["settings"]["vnext"][0] == {"address": "fin.example.com", "port": 8443, "users": [
    {"id": "11111111-2222-3333-4444-555555555555", "encryption": "none", "flow": "xtls-rprx-vision"}]}
rs = o["streamSettings"]["realitySettings"]
assert (rs["publicKey"], rs["shortId"], rs["serverName"], rs["spiderX"]) == ("PUBKEY", "ab12", "www.microsoft.com", "/")
x = vless2xray.build(LINK.replace("type=tcp", "type=xhttp&path=%2Fx&mode=packet-up").replace("&flow=xtls-rprx-vision", ""))
assert x["outbounds"][0]["streamSettings"]["xhttpSettings"] == {"path": "/x", "mode": "packet-up"}
assert "flow" not in x["outbounds"][0]["settings"]["vnext"][0]["users"][0]
for bad in ("http://x", "vless://u@h:1?type=ws&security=reality&pbk=a&sni=b", "vless://u@h:1?type=tcp&security=tls"):
    try: vless2xray.build(bad); raise SystemExit(f"должно отклоняться: {bad}")
    except ValueError: pass
print("✓ vless:// -> Xray config (tcp+vision, xhttp, отказ на чужих транспортах)")

from app.config import Settings  # noqa: E402
from app.main import make_bot  # noqa: E402


async def fake_socks(seen: list):
    async def h(r, w):
        ver, n = await r.readexactly(2); await r.readexactly(n)          # greeting
        w.write(b"\x05\x00"); await w.drain()
        ver, cmd, _, atyp = await r.readexactly(4)
        if atyp == 3:
            ln = (await r.readexactly(1))[0]; host = (await r.readexactly(ln)).decode()
        else:
            host = "IP:" + ".".join(str(b) for b in await r.readexactly(4))
        port = int.from_bytes(await r.readexactly(2), "big")
        seen.append((host, port)); w.close()                            # дальше не идём — нам важен адрес
    return await asyncio.start_server(h, "127.0.0.1", 0)


async def main():
    seen: list = []
    srv = await fake_socks(seen)
    port = srv.sockets[0].getsockname()[1]
    bot = make_bot(Settings(telegram_proxy=f"socks5://127.0.0.1:{port}"))
    try:
        await asyncio.wait_for(bot.get_me(), 10)
    except Exception:
        pass  # ожидаемо: фейковый прокси обрывает соединение
    await bot.session.close(); srv.close()
    assert seen and seen[0] == ("api.telegram.org", 443), seen
    print("✓ бот ходит через SOCKS5 и передаёт прокси имя api.telegram.org (DNS резолвит FIN, не РФ)")

asyncio.run(main())
print("\nВсе проверки пройдены")
