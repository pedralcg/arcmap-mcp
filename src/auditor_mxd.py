# -*- coding: utf-8 -*-
"""
auditor_mxd.py  --  Abre UN .mxd con arcpy y escupe su inventario en JSON.

Se ejecuta con el Python 2.7 de ArcGIS, en un proceso APARTE y con timeout
impuesto por quien lo llama. Ese aislamiento es el punto entero de este
fichero, no un detalle: el 2026-08-27 un .mxd cuyas fuentes de datos no
respondian dejo a arcpy colgado indefinidamente y, al haberlo lanzado dentro
del puente, ArcMap quedo inservible hasta matarlo por PID. Un documento que
cuelga debe llevarse por delante SOLO a su propio proceso.

Uso:
    python27 auditor_mxd.py "C:/ruta/al/plano.mxd"

Salida: una sola linea JSON por stdout. Nunca lanza: los fallos van dentro
del JSON, para que el que llama pueda distinguir "documento roto" de
"proceso muerto".
"""
import json
import sys


def texto(valor):
    """A unicode sin reventar: los mensajes de arcpy en castellano traen acentos
    y str() explota con 'ascii codec' en Python 2."""
    if valor is None:
        return None
    try:
        if isinstance(valor, unicode):  # noqa: F821 (Python 2)
            return valor
        return unicode(valor, "utf-8", "replace")  # noqa: F821
    except Exception:
        try:
            return unicode(repr(valor))  # noqa: F821
        except Exception:
            return u"<ilegible>"


def auditar(ruta):
    salida = {"ruta": ruta, "ok": False}
    import arcpy  # dentro, para que el fallo de import tambien salga en JSON

    mxd = arcpy.mapping.MapDocument(ruta)
    capas = []
    for lyr in arcpy.mapping.ListLayers(mxd):
        info = {"nombre": texto(getattr(lyr, "name", None))}
        try:
            info["rota"] = bool(lyr.isBroken)
        except Exception:
            info["rota"] = None
        try:
            info["grupo"] = bool(lyr.isGroupLayer)
        except Exception:
            info["grupo"] = None
        try:
            info["fuente"] = texto(lyr.dataSource) if lyr.supports("DATASOURCE") else None
        except Exception:
            # Una capa rota lanza al pedir dataSource: no es un fallo del auditor.
            info["fuente"] = None
        try:
            dq = lyr.definitionQuery if lyr.supports("DEFINITIONQUERY") else None
            info["definition_query"] = texto(dq) if dq else None
        except Exception:
            info["definition_query"] = None
        capas.append(info)

    salida["capas"] = capas
    salida["num_capas"] = len(capas)
    salida["num_rotas"] = len([c for c in capas if c.get("rota")])
    salida["num_con_query"] = len([c for c in capas if c.get("definition_query")])
    try:
        salida["data_frames"] = [texto(df.name) for df in arcpy.mapping.ListDataFrames(mxd)]
    except Exception:
        salida["data_frames"] = None
    salida["ok"] = True
    del mxd
    return salida


def main():
    if len(sys.argv) < 2:
        sys.stdout.write(json.dumps({"ok": False, "error": "falta la ruta del .mxd"}))
        return 2
    ruta = sys.argv[1]
    try:
        salida = auditar(ruta)
    except Exception as exc:
        salida = {"ruta": ruta, "ok": False, "error": texto(exc),
                  "tipo_error": type(exc).__name__}
    sys.stdout.write(json.dumps(salida, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
