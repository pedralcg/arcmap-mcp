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


def _numero(valor):
    try:
        return float(valor)
    except Exception:
        return None


def marco_en_pagina(df, ancho_pagina, alto_pagina):
    """Posicion del marco y si cae en la hoja: "dentro", "parcial" o "fuera".

    Un "Nuevo marco de datos" arrastrado fuera de la hoja sigue en el documento
    con sus capas y sus filtros, pero no sale en el plano. En el bloque 03 de
    ID2018 hubo 77 asi, y un auditor que no lo mire los reporta como errores.
    `en_pagina` es None si falta algun dato para decidirlo: no se adivina.
    """
    info = {"x": _numero(getattr(df, "elementPositionX", None)),
            "y": _numero(getattr(df, "elementPositionY", None)),
            "ancho": _numero(getattr(df, "elementWidth", None)),
            "alto": _numero(getattr(df, "elementHeight", None))}
    x, y, w, h = info["x"], info["y"], info["ancho"], info["alto"]
    if None in (x, y, w, h, ancho_pagina, alto_pagina):
        info["en_pagina"] = None
    elif x >= 0 and y >= 0 and x + w <= ancho_pagina and y + h <= alto_pagina:
        info["en_pagina"] = "dentro"
    elif x < ancho_pagina and y < alto_pagina and x + w > 0 and y + h > 0:
        info["en_pagina"] = "parcial"
    else:
        info["en_pagina"] = "fuera"
    return info


def _y(a, b):
    """AND con tres valores: None si no se puede decidir, sin inclinarse a un lado."""
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def capas_del_marco(capas_arcpy, marco, se_ve_marco):
    """Las capas de UN marco, con su visibilidad propia, efectiva y en el plano.

    - `visible`: la casilla de la capa.
    - `visible_efectivo`: ella Y todos sus grupos encendidos.
    - `en_plano`: ademas, su marco cae (al menos en parte) en la hoja.
    None en cualquiera de las tres = no se pudo leer; ni se da por visible ni
    por apagada.

    `ListLayers(mxd, "", df)` las da en el orden del TOC, en profundidad: cada
    grupo va seguido de su contenido. Se lleva una pila de grupos abiertos y el
    padre de una capa es el ultimo grupo cuyo longName es prefijo del suyo. Por
    orden y no por nombre: dos grupos que se llamen igual en ramas distintas no
    se confunden.
    """
    salida = []
    pila = []  # [(longName del grupo, visible_efectivo del grupo)]
    for lyr in capas_arcpy:
        info = {"nombre": texto(getattr(lyr, "name", None)), "data_frame": marco}
        try:
            info["nombre_largo"] = texto(lyr.longName)
        except Exception:
            info["nombre_largo"] = info["nombre"]
        largo = info["nombre_largo"] or u""
        while pila and not largo.startswith(pila[-1][0] + u"\\"):
            pila.pop()
        visible_padres = pila[-1][1] if pila else True
        try:
            info["visible"] = bool(lyr.visible)
        except Exception:
            info["visible"] = None
        info["visible_efectivo"] = _y(info["visible"], visible_padres)
        info["en_plano"] = _y(info["visible_efectivo"], se_ve_marco)
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
        if info["grupo"]:
            pila.append((largo, info["visible_efectivo"]))
        salida.append(info)
    return salida


def resumir(capas):
    """Dos numeros de cada cosa, y el que importa para el plano es el `_en_plano`:
    en el bloque 02 de ID2018 habia 2.335 capas rotas y solo 87 se dibujaban.
    Los grupos no cuentan (ni se rompen ni llevan filtro)."""
    datos = [c for c in capas if not c.get("grupo")]
    return {
        "num_capas": len(capas),
        "num_rotas": len([c for c in datos if c.get("rota")]),
        "num_rotas_en_plano": len([c for c in datos if c.get("rota") and c.get("en_plano")]),
        "num_con_query": len([c for c in datos if c.get("definition_query")]),
        "num_con_query_en_plano": len(
            [c for c in datos if c.get("definition_query") and c.get("en_plano")]),
        # Lo que no se pudo decidir se cuenta aparte en vez de caer en un lado.
        "num_en_plano_desconocido": len([c for c in datos if c.get("en_plano") is None]),
    }


def auditar(ruta):
    salida = {"ruta": ruta, "ok": False}
    import arcpy  # dentro, para que el fallo de import tambien salga en JSON

    mxd = arcpy.mapping.MapDocument(ruta)
    try:
        ancho_pagina = _numero(mxd.pageSize.width)
        alto_pagina = _numero(mxd.pageSize.height)
    except Exception:
        ancho_pagina = alto_pagina = None

    capas = []
    marcos = []
    # Capa a capa POR MARCO: `ListLayers(mxd)` a secas las mezcla todas y no dice
    # de que marco es cada una. Los marcos se identifican por nombre: comparar con
    # `is` falla siempre en arcpy (cada llamada devuelve un objeto nuevo).
    for df in arcpy.mapping.ListDataFrames(mxd):
        info_df = {"nombre": texto(df.name)}
        info_df.update(marco_en_pagina(df, ancho_pagina, alto_pagina))
        en_pagina = info_df["en_pagina"]
        se_ve_marco = None if en_pagina is None else en_pagina != "fuera"
        capas_df = capas_del_marco(arcpy.mapping.ListLayers(mxd, "", df),
                                   info_df["nombre"], se_ve_marco)
        info_df["num_capas"] = len(capas_df)
        marcos.append(info_df)
        capas.extend(capas_df)

    salida["capas"] = capas
    salida["marcos"] = marcos
    salida["data_frames"] = [m["nombre"] for m in marcos]
    salida["pagina"] = {"ancho": ancho_pagina, "alto": alto_pagina}
    salida.update(resumir(capas))
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
