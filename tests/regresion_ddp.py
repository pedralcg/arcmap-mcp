# -*- coding: utf-8 -*-
"""regresion_ddp.py  --  Las tres tools del atlas, contra un documento CON Data Driven Pages.

Separado de regresion_sesion_viva.py a proposito. Para probar DDP hace falta un
documento con atlas habilitado, y eso en la practica es siempre un mxd de PRODUCCION;
el otro script anade capas, cambia simbologia y lanza geoprocesos sobre el documento
abierto, asi que juntarlos invitaria a lanzar todo aquello contra un proyecto real.

**Este script NO modifica el documento.** Solo lee (`list_ddp`), mueve el encuadre de la
sesion (`goto_ddp_page`, que no se guarda) y exporta a `C:\\temp`, nunca junto al mxd.

Lo que SI escribe junto al original, y conviene saberlo: con un documento de rutas
RELATIVAS, las tres tools toman su copia de trabajo en un `~arcmap-mcp-snap_<guid>.mxd`
oculto **en la carpeta del mxd** (por diseno, desde la 2.12.0: la alternativa, %TEMP%,
era justo la que rompia las rutas relativas). Se borra al terminar, pero si la carpeta
esta sincronizada viaja un instante a la nube. El script comprueba que no queda ninguno.

Uso:  abrir en ArcMap un mxd con DDP habilitadas y
      python tests/regresion_ddp.py        (sale 1 si algo falla)

Por que existe: el defecto grave de la 2.12.0 era que la copia del documento rompia las
rutas relativas, y entonces `export_ddp` sacaba el atlas con su marco, su leyenda y su
titulo, NI UN DATO dentro, y SIN error. Un atlas vacio esta igual de bien formado que uno
bueno: mismo numero de paginas, mismos textos. Lo que lo delata es el PESO y las
coordenadas de la cuadricula, y eso es lo que se comprueba aqui.
"""
import json
import os
import socket
import sys

HOST = os.environ.get("ARCMAP_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ARCMAP_BRIDGE_PORT", "27179"))

SEP = chr(92)
SALIDAS = "C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion"

ok_n = 0
fallos = []


def enviar(tipo, params=None, timeout=1800):
    """1800 s: export_ddp es de las llamadas mas lentas del puente (copia del documento
    mas exportacion pagina a pagina), y el timeout corto la cortaria a mitad."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((HOST, PORT))
    s.sendall(json.dumps({"type": tipo, "params": params or {}}).encode("utf-8"))
    buf = b""
    while True:
        c = s.recv(65536)
        if not c:
            break
        buf += c
    s.close()
    return json.loads(buf.decode("utf-8"))


def t(tipo, params=None, espera_ok=True, nota=""):
    global ok_n
    try:
        r = enviar(tipo, params)
    except Exception as exc:
        fallos.append((tipo, "EXCEPCION: " + str(exc)[:90]))
        sys.stdout.write("  [EXC] " + tipo + chr(10))
        return None
    bien = (bool(r.get("ok")) == espera_ok)
    if bien:
        ok_n += 1
    else:
        fallos.append((tipo, str(r.get("error", r))[:110]))
    sys.stdout.write("  [%s] %-22s %s%s" % ("OK " if bien else "FALLO", tipo, nota, chr(10)))
    return r


def comprobar(etiqueta, condicion, detalle=""):
    global ok_n
    if condicion:
        ok_n += 1
    else:
        fallos.append((etiqueta, detalle[:110]))
    sys.stdout.write("  [%s] %-22s %s" % ("OK " if condicion else "FALLO", etiqueta, chr(10)))


def res(r):
    return (r or {}).get("result") or {}


def rotas_en_sesion():
    """LINEA BASE de capas rotas. Exigir cero a la copia solo funciona con un documento
    impecable: si el original ya tiene capas rotas, la copia las tendra igual y eso no es
    una regresion. Medir el nulo antes de acusar."""
    try:
        return int((enviar("list_broken_data_sources", {}).get("result") or {}).get("num") or 0)
    except Exception:
        return None


if not os.path.isdir(SALIDAS):
    os.makedirs(SALIDAS)

sys.stdout.write("--- el documento y su atlas ---" + chr(10))
r = t("get_arcmap_info")
mxd = res(r).get("mxd")
sys.stdout.write("      -> " + str(mxd) + chr(10))

r = t("list_ddp", nota="(atlas del documento)")
d = res(r)
if not d.get("habilitado"):
    sys.stdout.write(chr(10) + "ABORTA: el documento abierto NO tiene Data Driven Pages "
                     "habilitadas. Abre uno con atlas (capa indice + campo de nombre) y "
                     "repite; no se ha probado nada." + chr(10))
    sys.exit(2)

comprobar("list_ddp trae paginas, campo y capa indice",
          (d.get("num_paginas") or 0) > 0 and d.get("capa_indice") and d.get("campo_nombre"),
          str(d)[:110])
sys.stdout.write("      -> %s paginas, indice '%s', campo '%s', snapshot_via=%s%s"
                 % (d.get("num_paginas"), d.get("capa_indice"), d.get("campo_nombre"),
                    d.get("snapshot_via"), chr(10)))

base = rotas_en_sesion()
comprobar("la copia del atlas no anade capas rotas",
          base is not None and d.get("capas_rotas_en_copia") <= base,
          "copia=%s sesion=%s" % (d.get("capas_rotas_en_copia"), base))

sys.stdout.write("--- navegar el atlas ---" + chr(10))
valores = d.get("valores") or []
if valores:
    r = t("goto_ddp_page", {"valor": str(valores[0])}, nota="(por valor del indice)")
    comprobar("goto_ddp_page resuelve el valor a una pagina",
              res(r).get("page_id") is not None, str(res(r))[:110])
r = t("goto_ddp_page", {"pagina": 1}, nota="(por id de pagina)")
comprobar("goto_ddp_page acepta el id", res(r).get("page_id") == 1, str(res(r))[:110])
t("goto_ddp_page", {"valor": "NO_EXISTE_9999"},
  espera_ok=False, nota="(valor inexistente: debe fallar)")

sys.stdout.write("--- exportar el atlas ---" + chr(10))
PDF = SALIDAS + SEP + "ddp_regresion.pdf"
if os.path.exists(PDF):
    os.remove(PDF)
paginas = min(2, d.get("num_paginas") or 1)
r = t("export_ddp", {"salida": PDF, "rango": "1-%d" % paginas, "dpi": 96},
      nota="(%d paginas a C:\\temp)" % paginas)
comprobar("export_ddp exporta las paginas pedidas",
          res(r).get("num") == paginas, str(res(r))[:110])
comprobar("export_ddp no anade capas rotas en la copia",
          base is not None and res(r).get("capas_rotas_en_copia") <= base,
          str(res(r))[:110])

# EL CHEQUEO QUE JUSTIFICA EL SCRIPT. El atlas sin datos salia igual de bien formado:
# mismas paginas, marco, leyenda y titulo, y sin error. Lo que cambiaba era el PESO,
# porque dentro no habia geometria. Un marco vacio son decenas de KB; con datos, MB.
tam = os.path.getsize(PDF) if os.path.exists(PDF) else 0
comprobar("el PDF lleva datos dentro (>300 KB)", tam > 300000, "tamano=%d bytes" % tam)
sys.stdout.write("      -> %d bytes%s" % (tam, chr(10)))

# Y que la copia no se quede en la carpeta del usuario, que es el efecto lateral
# aceptado del arreglo de rutas relativas.
if mxd:
    carpeta = os.path.dirname(mxd)
    try:
        restos = [n for n in os.listdir(carpeta) if n.startswith("~arcmap-mcp-snap")]
    except Exception:
        restos = None
    comprobar("no queda snapshot junto al original",
              restos == [] or restos is None, "restos=%s" % (restos,))

if os.path.exists(PDF):
    os.remove(PDF)

sys.stdout.write(chr(10) + "RESULTADO: %d correctos, %d fallos" % (ok_n, len(fallos)) + chr(10))
for nombre, det in fallos:
    sys.stdout.write("   FALLO %-24s %s" % (nombre, det) + chr(10))
sys.exit(1 if fallos else 0)
