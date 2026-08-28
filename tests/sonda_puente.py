# -*- coding: utf-8 -*-
"""
sonda_puente.py  ──  Sonda MANUAL del puente de ArcMap, sin cliente MCP por medio.

NO es una suite de tests, aunque se llamara `test_bridge.py` hasta el 2026-08-27:
es una herramienta de diagnostico que se lanza a mano y que EXIGE ArcMap abierto
con el puente arrancado. Se renombro porque `unittest discover` la recogia como
test, la ejecutaba al importarla y tumbaba la suite entera en cualquier maquina
sin ArcMap.

Los tests automaticos del protocolo, que si corren sin ArcMap, estan en
`test_client_protocol.py`.

Uso (Python 3, solo stdlib; vale cualquier python del PATH):
    python sonda_puente.py ping
    python sonda_puente.py get_arcmap_info
    python sonda_puente.py list_layers
    python sonda_puente.py zoom_to_layer nombre=NOMBRE_DE_TU_CAPA
    python sonda_puente.py zoom_to_layer "nombre=Capa Con Espacios"
    python sonda_puente.py export_pdf salida=C:/temp/plano.pdf dpi=300

Los parametros van como pares clave=valor (evita el infierno de comillas de
PowerShell). Tambien se acepta un unico argumento JSON si empieza por '{'.

Requiere que el puente este levantado dentro de ArcMap (boton Iniciar de la
barra arcmap-mcp).
"""
import os
import sys
import json
import socket

# Mismas variables que el servidor y el add-in: si se cambio el puerto, la sonda
# tiene que seguirlo o dara "conexion rechazada" contra un puente que si esta vivo.
HOST = os.environ.get("ARCMAP_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ARCMAP_BRIDGE_PORT", "27179"))

cmd = sys.argv[1] if len(sys.argv) > 1 else "ping"

# Parametros: pares clave=valor, o un unico arg JSON si empieza por '{'.
params = {}
extra = sys.argv[2:]
if len(extra) == 1 and extra[0].lstrip().startswith("{"):
    params = json.loads(extra[0])
else:
    for tok in extra:
        if "=" in tok:
            k, v = tok.split("=", 1)
            # convertir enteros simples (p.ej. dpi=300)
            params[k] = int(v) if v.isdigit() else v

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(20)
try:
    s.connect((HOST, PORT))
except Exception as e:
    print("NO HAY PUENTE en %s:%s -> %s" % (HOST, PORT, e))
    print("Abre ArcMap y pulsa Iniciar en la barra arcmap-mcp.")
    sys.exit(1)

s.sendall(json.dumps({"type": cmd, "params": params}).encode("utf-8"))

buf = b""
resp = None
while True:
    chunk = s.recv(65536)
    if not chunk:
        break
    buf += chunk
    try:
        resp = json.loads(buf.decode("utf-8"))
        break
    except ValueError:
        continue
s.close()

if resp is None:
    print("Sin respuesta valida del puente.")
    sys.exit(2)

print(json.dumps(resp, ensure_ascii=False, indent=2))
