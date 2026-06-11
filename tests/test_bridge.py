# -*- coding: utf-8 -*-
"""
test_bridge.py  ──  Prueba el puente de ArcMap SIN pasar por ningun cliente MCP.

Uso (Python 3, solo stdlib; vale cualquier python del PATH):
    python test_bridge.py ping
    python test_bridge.py get_arcmap_info
    python test_bridge.py list_layers
    python test_bridge.py zoom_to_layer nombre=NOMBRE_DE_TU_CAPA
    python test_bridge.py zoom_to_layer "nombre=Capa Con Espacios"
    python test_bridge.py export_pdf salida=C:/temp/plano.pdf dpi=300

Los parametros van como pares clave=valor (evita el infierno de comillas de
PowerShell). Tambien se acepta un unico argumento JSON si empieza por '{'.

Requiere que el puente este levantado dentro de ArcMap (boton Iniciar de la
barra arcmap-mcp).
"""
import sys
import json
import socket

HOST = "127.0.0.1"
PORT = 27179

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
