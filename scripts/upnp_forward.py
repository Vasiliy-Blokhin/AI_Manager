# -*- coding: utf-8 -*-
"""Проброс портов на роутере через UPnP (SSDP + SOAP, только stdlib).

Использование:
    python scripts/upnp_forward.py add 443 443      # внешний 443 -> локальный 443 (TCP)
    python scripts/upnp_forward.py add 80 80
    python scripts/upnp_forward.py list
    python scripts/upnp_forward.py remove 443
"""
import sys
import re
import socket
import urllib.request
import xml.etree.ElementTree as ET


SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_MX = 3


def ssdp_discover():
    """Найти IGD роутера, вернуть (control_url, service_type)."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {SSDP_MX}\r\n"
        "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
        "\r\n"
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(SSDP_MX + 2)
    locations = []
    for _ in range(3):
        try:
            sock.sendto(msg.encode(), SSDP_ADDR)
            while True:
                data, _ = sock.recvfrom(4096)
                m = re.search(rb"(?i)^LOCATION:\s*(\S+)", data, re.M)
                if m:
                    loc = m.group(1).decode().strip()
                    if loc not in locations:
                        locations.append(loc)
        except socket.timeout:
            break
    sock.close()
    if not locations:
        raise SystemExit("IGD не найден: UPnP выключен на роутере или ответ не получен.")

    # Ищем сервис WANIPConnection (или WANPPPConnection) в описании устройства
    for loc in locations:
        try:
            xml = urllib.request.urlopen(loc, timeout=8).read()
        except Exception:
            continue
        root = ET.fromstring(xml)
        ns = {"u": "urn:schemas-upnp-org:device-1-0"}
        for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
            st = (svc.findtext("u:serviceType", namespaces=ns) or "")
            if "WANIPConnection" in st or "WANPPPConnection" in st:
                ctrl = svc.findtext("u:controlURL", namespaces=ns)
                base = loc.split("/")[:3]
                control_url = "/".join(base) + ctrl
                return control_url, st
    raise SystemExit("Сервис WANIPConnection не найден в описаниях IGD.")


def soap_call(control_url, service_type, action, args):
    body_args = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    body = (
        '<?xml version="1.0"?>'
        f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body><u:{action} xmlns:u='{service_type}'>{body_args}</u:{action}></s:Body></s:Envelope>"
    )
    req = urllib.request.Request(
        control_url,
        data=body.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service_type}#{action}"',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"SOAP {action}: HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    ctrl, st = ssdp_discover()
    print(f"IGD: {ctrl}")

    if cmd == "list":
        out = soap_call(ctrl, st, "GetGenericPortMappingEntry", {"NewPortMappingIndex": 0})
        i = 0
        while True:
            try:
                out = soap_call(ctrl, st, "GetGenericPortMappingEntry", {"NewPortMappingIndex": i})
            except SystemExit:
                break
            ext = re.search(r"<NewExternalPort>(\d+)", out)
            proto = re.search(r"<NewProtocol>(\w+)", out)
            internal = re.search(r"<NewInternalClient>([\d.]+)", out)
            desc = re.search(r"<NewPortMappingDescription>(.*?)</", out)
            if ext:
                print(f"  {ext.group(1)}/{proto.group(1) if proto else '?'} -> "
                      f"{internal.group(1) if internal else '?'} ({desc.group(1) if desc else ''})")
            i += 1
            if i > 50:
                break
    elif cmd == "add":
        ext_port = int(sys.argv[2]) if len(sys.argv) > 2 else 443
        int_port = int(sys.argv[3]) if len(sys.argv) > 3 else ext_port
        desc = sys.argv[4] if len(sys.argv) > 4 else f"AI-Manager-{ext_port}"
        ip = local_ip()
        soap_call(ctrl, st, "AddPortMapping", {
            "NewRemoteHost": "",
            "NewExternalPort": ext_port,
            "NewProtocol": "TCP",
            "NewInternalPort": int_port,
            "NewInternalClient": ip,
            "NewEnabled": 1,
            "NewPortMappingDescription": desc,
            "NewLeaseDuration": 0,
        })
        print(f"OK: внешний {ext_port}/TCP -> {ip}:{int_port} ({desc})")
    elif cmd == "remove":
        ext_port = int(sys.argv[2])
        soap_call(ctrl, st, "DeletePortMapping", {
            "NewRemoteHost": "",
            "NewExternalPort": ext_port,
            "NewProtocol": "TCP",
        })
        print(f"OK: удалён проброс {ext_port}/TCP")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
