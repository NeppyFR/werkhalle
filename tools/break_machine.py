"""Breaks a machine from outside the MES: an independent OPC UA client that calls InjectFault on the plant directly.

    python tools/break_machine.py PR2 --code P-204 --repair 240 --port 4840

Deliberately imports nothing from werkhalle. The MES learns about the fault the same way it would from a real machine,
through the DataChange, with no idea who caused it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from asyncua import Client, ua

NAMESPACE = "urn:werkhalle:plant"


async def break_machine(machine: str, code: str, repair_min: float, port: int) -> dict:
    client = Client(f"opc.tcp://127.0.0.1:{port}/werkhalle/", timeout=10)
    client.application_uri = "urn:werkhalle:break-machine"
    await client.connect()
    try:
        namespace = await client.get_namespace_index(NAMESPACE)
        node = client.get_node(ua.NodeId(f"Werkhalle.{machine}", namespace))
        raw = await node.call_method(
            ua.NodeId(f"Werkhalle.{machine}.InjectFault", namespace),
            ua.Variant(code, ua.VariantType.String),
            ua.Variant(float(repair_min), ua.VariantType.Double),
        )
    finally:
        await client.disconnect()
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Löst per OPC UA eine Störung an einer Maschine aus")
    parser.add_argument("machine", help="Maschinenkennung, zum Beispiel PR2")
    parser.add_argument("--code", default="", help="Störcode, leer = Standardcode der Maschine")
    parser.add_argument("--repair", type=float, default=0.0, help="Reparaturdauer in Simulationsminuten, 0 = Mittelwert des Codes")
    parser.add_argument("--port", type=int, default=4840)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(break_machine(args.machine, args.code, args.repair, args.port))
    except (OSError, asyncio.TimeoutError, ua.UaError) as exc:
        print(f"Anlage auf Port {args.port} nicht erreichbar oder Maschine {args.machine} unbekannt: {exc!r}", file=sys.stderr)
        return 2
    if result.get("ok"):
        print(f"{args.machine}: Störung ausgelöst ({args.code or 'Standardcode'}).")
        return 0
    print(f"{args.machine}: Anlage lehnt ab: {result.get('reason')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
