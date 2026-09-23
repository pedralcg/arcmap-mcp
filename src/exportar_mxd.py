# -*- coding: utf-8 -*-
"""
exportar_mxd.py  --  Exporta el layout de UN .mxd a JPG o PDF, en su propio proceso.

Lo lanza la herramienta `export_mxd_lote` del servidor, con el Python 2.7 de ArcGIS,
UN proceso por documento. Ese aislamiento es la razón de existir de este fichero:

- Un proceso arcpy que ya ha exportado un layout NO vuelve a exportar otro. Medido
  el 2026-09-22 sobre planos de 94-141 capas: en un mismo proceso salió el PRIMER
  export y fallaron los cuatro siguientes con "PageLayoutObject: error durante
  ejecución de ExportToJPEG", aunque el proceso no hubiera guardado nada. Un proceso
  nuevo por documento: 7 de 7, y luego tandas de 14 sin un fallo.
- Exportar así funciona CON ArcMap abierto (comprobado ese mismo día), a diferencia
  de lo que se creía por la contención de licencia.

Uso:
    python27 exportar_mxd.py <trabajo.json>
    trabajo = {"mxd": ruta, "salida": ruta, "formato": "jpg"|"pdf", "dpi": int}

El trabajo va en un JSON UTF-8 y no en argumentos a propósito: en Python 2 sobre
Windows `sys.argv` llega en bytes de la codepage ANSI, y una tilde en la ruta es
justo el caso normal en este trabajo (ver `ruta_unicode` en auditor_mxd.py).

Salida: una sola línea JSON ASCII por stdout. Nunca lanza: los fallos van dentro.
El export se hace a un TEMPORAL junto al destino y solo al final se mueve encima,
así que un fallo a mitad no se lleva por delante el plano que ya había.
"""
import io
import json
import os
import sys
import time
import traceback
import uuid


def _responder(datos):
    sys.stdout.write(json.dumps(datos, ensure_ascii=True))
    sys.stdout.flush()


def _texto(ex):
    try:
        return unicode(ex)  # noqa: F821 (Python 2)
    except Exception:
        return repr(ex)


def main():
    inicio = time.time()
    try:
        with io.open(sys.argv[1], "r", encoding="utf-8") as f:
            trabajo = json.loads(f.read())
        ruta = trabajo["mxd"]
        salida = trabajo["salida"]
        formato = trabajo.get("formato", "jpg").lower()
        dpi = int(trabajo.get("dpi", 230))
    except Exception as ex:
        _responder({"ok": False, "fase": "leer trabajo", "error": _texto(ex)})
        return 1

    base, ext = os.path.splitext(salida)
    tmp = u"%s.mcp-%s%s" % (base, uuid.uuid4().hex[:8], ext)
    fase = u"importando arcpy"
    try:
        import arcpy.mapping as MAP
        fase = u"abriendo documento"
        mxd = MAP.MapDocument(ruta)
        fase = u"exportando"
        if formato == "pdf":
            MAP.ExportToPDF(mxd, tmp, resolution=dpi)
        else:
            MAP.ExportToJPEG(mxd, tmp, resolution=dpi)
        del mxd
        fase = u"moviendo al destino"
        if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            raise RuntimeError(u"arcpy no dio error pero no escribió nada")
        sobrescrito = os.path.exists(salida)
        if sobrescrito:
            os.remove(salida)
        os.rename(tmp, salida)
    except Exception as ex:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        _responder({"ok": False, "fase": fase, "error": _texto(ex),
                    "traceback": traceback.format_exc().decode("utf-8", "replace"),
                    "segundos": round(time.time() - inicio, 1)})
        return 1

    _responder({"ok": True, "salida": salida, "bytes": os.path.getsize(salida),
                "sobrescrito": sobrescrito, "segundos": round(time.time() - inicio, 1)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
