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
import os
import sys


def ruta_unicode(bruto):
    """La ruta del argumento, a unicode, decidiendo la codificacion UNA vez.

    En Python 2 sobre Windows `sys.argv` llega en BYTES, y con una tilde o una
    ñ esos bytes NO son UTF-8: desde cmd/PowerShell son los de la codepage ANSI
    (cp1252 aqui). Si se arrastran tal cual hasta el `json.dumps` final, el
    encoder los decodifica como UTF-8 y lanza UnicodeDecodeError FUERA de todo
    try: el proceso muere con traceback por stderr, stdout sale VACIO y
    `audit_folder` marca "sin salida" en todos los documentos de la carpeta.
    Reproducido el 2026-09-20 con "...\\Cartografia\\plano.mxd": byte 0xed.

    No hay una sola respuesta correcta, porque el mismo `str` puede venir en
    cp1252 (cmd, PowerShell, el subprocess de audit_folder) o en UTF-8 (un
    shell tipo MSYS / Git Bash). Se prueban las dos y MANDA LA QUE EXISTE EN
    DISCO: es el unico arbitro fiable. Si ninguna existe -el caso de un fichero
    que de verdad no esta-, se devuelve la primera, que dara un error legible
    con la ruta escrita como la escribio quien llamo.
    """
    if isinstance(bruto, unicode):  # noqa: F821 (Python 2)
        return bruto
    candidatos = []
    vistos = set()
    for codec in (sys.getfilesystemencoding(), "mbcs", "utf-8", "latin-1"):
        if not codec or codec in vistos:
            continue
        vistos.add(codec)
        try:
            candidatos.append(bruto.decode(codec))
        except Exception:
            continue
    for candidato in candidatos:
        try:
            if os.path.exists(candidato):
                return candidato
        except Exception:
            continue
    if candidatos:
        return candidatos[0]
    return bruto.decode("latin-1", "replace")


def texto(valor):
    """A unicode sin reventar: los mensajes de arcpy en castellano traen acentos
    y str() explota con 'ascii codec' en Python 2."""
    if valor is None:
        return None
    try:
        if isinstance(valor, unicode):  # noqa: F821 (Python 2)
            return valor
        if isinstance(valor, BaseException):
            # unicode(exc, "utf-8") lanza TypeError SIEMPRE (no es un buffer), asi que
            # todo error caia al repr() de abajo y salia como
            # "RuntimeError(u'... no v\xe1lido.',)", con los acentos escapados. El
            # mensaje de verdad esta en los args.
            partes = [texto(a) for a in valor.args] or [texto(type(valor).__name__)]
            return u"%s: %s" % (type(valor).__name__, u" ".join(p for p in partes if p))
        if isinstance(valor, str):
            try:
                return valor.decode("utf-8")
            except UnicodeDecodeError:
                return valor.decode("mbcs", "replace")
        return unicode(valor)  # noqa: F821
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
    """Nunca lanza. Quien llama distingue "documento roto" de "proceso muerto"
    por el JSON, y para eso el JSON tiene que salir SIEMPRE: hasta el
    `json.dumps` final va dentro de un try, con un sobre de ultimo recurso en
    ASCII puro que no puede fallar al codificar."""
    if len(sys.argv) < 2:
        sys.stdout.write(json.dumps({"ok": False, "error": "falta la ruta del .mxd"}))
        return 2
    try:
        ruta = ruta_unicode(sys.argv[1])
    except Exception as exc:
        ruta = u"<ruta ilegible>"
        salida = {"ruta": ruta, "ok": False,
                  "error": u"no se pudo interpretar la ruta recibida: %s" % texto(exc),
                  "tipo_error": type(exc).__name__}
    else:
        try:
            salida = auditar(ruta)
        except Exception as exc:
            salida = {"ruta": ruta, "ok": False, "error": texto(exc),
                      "tipo_error": type(exc).__name__}
    try:
        # ensure_ascii=True devuelve `str` ASCII puro: escribirlo en una consola
        # cp1252 o en un pipe no puede fallar por codificacion.
        sys.stdout.write(json.dumps(salida, ensure_ascii=True))
    except Exception:
        sys.stdout.write(
            '{"ok": false, "error": "el auditor no pudo serializar su salida"}')
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
