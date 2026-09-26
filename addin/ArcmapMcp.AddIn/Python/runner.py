# -*- coding: utf-8 -*-
"""Runner arcpy OUT-OF-PROCESS del add-in .NET.

Ejecuta en Python 2.7 standalone (C:\\Python27\\ArcGIS10.5\\python.exe) las
operaciones que ArcObjects .NET no cubre: execute_code (arcpy arbitrario), las
3 de Data Driven Pages (solo existen en arcpy) y las 5 ambientales (lógica
arcpy pura, sin añadir-al-mapa: eso lo hace el add-in nativamente sobre la
sesión viva al volver).

Contrato (evita la consola cp1252: NUNCA datos por stdout):
    python runner.py <job.json> <out.json>
    job  = {"op": str, "params": {...}, "mxd": ruta_snapshot_opcional}
    out  = {"ok": true, "result": {...}} | {"ok": false, "error": str, "traceback": str}

El "mxd" es una COPIA (save_mxd_as) de la sesión viva: las operaciones de
documento (execute_code, DDP) leen el estado real de la sesión pero sus
cambios al documento se descartan; las escrituras a DATOS en disco sí son
reales (mismas fuentes que la sesión).
"""
import io
import json
import os
import re
import sys
import time
import traceback
import uuid

# --------------------------------------------------------------------------- #
# Traza de fase.
#
# El add-in solo puede medir reloj de pared sobre este proceso: desde fuera, un
# runner atascado abriendo un documento y uno reclasificando un ráster de 4 GB
# son el mismo python.exe que no termina. Por eso el runner DICE en qué fase
# está, en un fichero al lado del out. Si salta el timeout, el add-in lee ese
# fichero y el error nombra el punto exacto donde se quedó, en vez de un
# "timeout" mudo. Coste: escribir ~80 bytes cuatro o cinco veces por job.
#
# La ruta se DERIVA del out (out.json -> out.json.fase) para no cambiar el
# contrato de argumentos del runner.
# --------------------------------------------------------------------------- #

_FASE_PATH = sys.argv[2] + ".fase" if len(sys.argv) > 2 else None


def _fase(nombre, detalle=None):
    """Anota la fase actual. NUNCA puede romper el job: si falla, se ignora."""
    if not _FASE_PATH:
        return
    try:
        datos = {"fase": nombre, "ts": time.time()}
        if detalle:
            datos["detalle"] = detalle
        # Mismo cuidado que en _serializar: json.dumps de Py2 con
        # ensure_ascii=False devuelve str si todo es ASCII, y io.open exige unicode.
        texto = json.dumps(datos, ensure_ascii=False)
        if isinstance(texto, str):
            texto = texto.decode("utf-8")
        with io.open(_FASE_PATH, "w", encoding="utf-8") as f:
            f.write(texto)
    except Exception:
        pass


# `import arcpy` NO es gratis: son segundos en frío, y en una máquina con la
# licencia en un servidor lento puede irse mucho más. Se anota ANTES de importar
# para que un cuelgue en el propio import también quede identificado.
_fase(u"importando arcpy")

import arcpy
from arcpy import mapping as MAP

_fase(u"arcpy importado")


def _u(x):
    """Coerción a unicode segura para Python 2.7 (str utf-8 o latin-1)."""
    if x is None:
        return None
    if isinstance(x, unicode):
        return x
    if isinstance(x, str):
        try:
            return x.decode("utf-8")
        except Exception:
            return x.decode("latin-1", "replace")
    return unicode(x)


class _BufferUnicode(object):
    """stdout de reemplazo que guarda TODO como unicode, coaccionando en cada write.

    `StringIO.StringIO` acumula lo que le echen sin mirar, y solo al final, en
    `getvalue()`, intenta juntarlo: si el codigo del usuario mezcla
    `print "Numero"` (str, bytes) con `print u"..."` (unicode), esa union
    concatena bytes y unicode con el codec ascii y lanza UnicodeDecodeError.
    El dano es desproporcionado: el codigo habia ido BIEN y tenia su RESULT
    asignado, y se perdia entero por una linea de traza. Coaccionando con `_u`
    en cada write el problema no llega a existir.
    """

    def __init__(self):
        self._partes = []
        # El `print` de Python 2 lee y escribe este atributo en el fichero de
        # salida (StringIO tambien lo define). Sin el, print funciona igual
        # -CPython se traga el fallo del getattr- pero deja de separar los
        # `print a, b` con un espacio.
        self.softspace = 0

    def write(self, dato):
        self._partes.append(_u(dato))

    def writelines(self, lineas):
        for linea in lineas:
            self.write(linea)

    def flush(self):
        pass

    def getvalue(self):
        return u"".join(p for p in self._partes if p is not None)


def _resumen_stdout(stdout, maximo=500):
    """Cola del stdout capturado, para no perderlo cuando la respuesta es un error."""
    if not stdout:
        return u""
    if len(stdout) <= maximo:
        return u" Salida capturada: %s" % stdout
    return u" Ultimos %d caracteres de la salida: ...%s" % (maximo, stdout[-maximo:])


def _abrir_mxd(job):
    """Abre el snapshot del documento; error accionable si el job no lo trae.

    Es LA fase cara: medido el 2026-07-29, abrir un mxd de 5,9 MB costó 324 s
    frente a 6,4 s del job entero sin documento. Por eso se anota con el tamaño:
    si el timeout salta aquí, el mensaje ya dice por qué.
    """
    ruta = job.get("mxd")
    if not ruta:
        raise ValueError(u"Operación de documento sin snapshot .mxd (bug del add-in).")
    try:
        mb = os.path.getsize(ruta) / (1024.0 * 1024.0)
        detalle = u"%.1f MB" % mb
    except Exception:
        detalle = None
    _fase(u"abriendo documento", detalle)
    doc = MAP.MapDocument(_u(ruta))
    _fase(u"documento abierto", detalle)
    _contar_rotas(doc)
    return doc


# Fuentes rotas de la COPIA del documento. None = no se abrió ningún documento (o no
# se pudieron contar). Lo rellena _abrir_mxd y main() lo adjunta al resultado.
_ROTAS_EN_COPIA = None

# Ruta del .mxd ORIGINAL del que sale la copia (la pone el add-in en el job). Sirve
# para que el aviso de capas rotas diga de QUÉ documento habla.
_MXD_ORIGINAL = None

# False cuando execute_code recibió la copia pero el código no la usó (abría otros
# .mxd por ruta). Entonces el aviso de capas rotas habla de un documento que la
# llamada no ha tocado, y se omite.
_COPIA_USADA = True


def _contar_rotas(doc):
    """Cuenta las capas con la fuente rota EN LA COPIA. Nunca rompe el job.

    Red de seguridad de un fallo que fue silencioso de la 2.6.0 a la 2.11.0: la copia
    del .mxd se hacía en %TEMP%, y un documento de RUTAS RELATIVAS copiado a otra
    carpeta abre con todas sus capas rotas (reproducido en aislado el 2026-09-20).
    `export_ddp` exportaba entonces un atlas con leyenda y sin datos, sin un solo
    error. El add-in ya copia junto al original en ese caso; esto es el testigo que
    lo haría visible si volviera por otro camino. Es un contador, no un veredicto:
    el documento original también puede tener capas rotas de antes.
    """
    global _ROTAS_EN_COPIA
    try:
        _fase(u"comprobando fuentes de la copia")
        _ROTAS_EN_COPIA = [_u(l.name) for l in MAP.ListBrokenDataSources(doc)]
    except Exception:
        _ROTAS_EN_COPIA = None


def _nombre_original():
    if not _MXD_ORIGINAL:
        return u"el documento abierto en ArcMap"
    return u"%s (el documento abierto en ArcMap)" % os.path.basename(_u(_MXD_ORIGINAL))


def _adjuntar_rotas(resultado):
    """Añade al resultado el testigo de fuentes rotas de la copia, si lo hay.

    Se omite entero si el código no usó la copia: el 2026-09-22 las 20 llamadas de
    una sesión que editaba OTROS .mxd por ruta devolvieron, palabra por palabra, las
    18 capas rotas del documento abierto —que no se tocó ni una vez—, nombrando
    justo las capas que se estaban arreglando en los otros. Un aviso que no describe
    lo que hace la llamada se lee como si hablara de lo tuyo.
    """
    if _ROTAS_EN_COPIA is None or not isinstance(resultado, dict) or not _COPIA_USADA:
        return resultado
    resultado["capas_rotas_en_copia"] = len(_ROTAS_EN_COPIA)
    if _ROTAS_EN_COPIA:
        resultado["aviso_capas_rotas"] = (
            u"La copia de %s tiene %d capa(s) con la fuente ROTA: %s%s. Lo que dependa "
            u"de ellas sale sin esos datos. Si en la sesión están bien "
            u"(list_broken_data_sources), el fallo es de la copia: mira `snapshot_via`."
            % (_nombre_original(), len(_ROTAS_EN_COPIA), u", ".join(_ROTAS_EN_COPIA[:5]),
               u" y %d más" % (len(_ROTAS_EN_COPIA) - 5) if len(_ROTAS_EN_COPIA) > 5 else u""))
    return resultado


def _get_ddp(mxd):
    try:
        ddp = mxd.dataDrivenPages
    except Exception:
        ddp = None
    if ddp is None:
        raise ValueError(u"El documento no tiene Data Driven Pages habilitadas "
                         u"(Vista > Data Driven Pages en ArcMap).")
    return ddp


def _contar_paginas_rango(s):
    n = 0
    for parte in s.split(","):
        parte = parte.strip()
        if not parte:
            continue
        if "-" in parte:
            a, b = parte.split("-", 1)
            try:
                n += int(b) - int(a) + 1
            except ValueError:
                n += 1
        else:
            n += 1
    return n


def _checkout(ext):
    """Activa una extensión (Spatial/3D). Error accionable si no hay licencia."""
    try:
        estado = arcpy.CheckExtension(ext)
    except Exception:
        estado = u"(desconocido)"
    if estado != "Available":
        raise ValueError(u"Extensión '%s' no disponible (estado: %s). Revisa la "
                         u"licencia de la extensión." % (_u(ext), _u(estado)))
    arcpy.CheckOutExtension(ext)


def _existe(ruta):
    """¿Hay ya algo escrito en esa ruta? `arcpy.Exists` es el unico que ve
    dentro de una geodatabase (una feature class no es un fichero para
    os.path); os.path.exists queda de respaldo por si arcpy se atraganta."""
    if not ruta:
        return False
    try:
        if arcpy.Exists(_u(ruta)):
            return True
    except Exception:
        pass
    try:
        return os.path.exists(_u(ruta))
    except Exception:
        return False


def _preparar_salidas(params, rutas):
    """Decide QUE hacer si la salida ya existe, ANTES de gastar el geoproceso.

    `arcpy.env.overwriteOutput` viene a False en un proceso nuevo, asi que la
    segunda ejecucion de la misma operacion sobre la misma ruta moria con un
    ERROR 000725 despues de haber calculado el resultado entero: minutos de
    geoproceso tirados para acabar sin nada. Activarlo a ciegas seria peor: es
    global al proceso, y con el puesto una ruta mal tecleada borra en silencio
    el resultado de ayer. De ahi el parametro explicito `sobrescribir`, que por
    defecto NO sobrescribe y avisa antes de empezar.
    """
    sobrescribir = bool(params.get("sobrescribir", False))
    arcpy.env.overwriteOutput = sobrescribir
    if sobrescribir:
        return True
    ya_estan = [r for r in rutas if r and _existe(r)]
    if ya_estan:
        raise ValueError(u"La salida ya existe: %s. Borrala, cambia la ruta o "
                         u"repite con sobrescribir=true."
                         % u"; ".join(_u(r) for r in ya_estan))
    return False


def _es_geodatabase(carpeta):
    """¿La ruta de salida cae dentro de una gdb/mdb/sde? Cambia las reglas de
    nombrado: en carpeta, un raster sin extension es un GRID de Esri (nombre
    de 13 caracteres como mucho y sin espacios); en gdb no hay tal limite."""
    partes = _u(carpeta or u"").replace(u"/", u"\\").split(u"\\")
    return any(p.lower().endswith((u".gdb", u".mdb", u".sde")) for p in partes)


def _ruta_backlink(out_dir):
    """Ruta del raster de backlink (subproducto de CostDistance), con nombre UNICO.

    Con el nombre fijo `lcp_backlink` la segunda ruta de minimo coste calculada
    en la misma carpeta chocaba con el backlink de la primera y moria sin haber
    tocado la salida que el usuario pidio. En carpeta se escribe .tif a
    proposito: un GRID corta el nombre a 13 caracteres y ahi no cabe el sufijo.
    """
    marca = uuid.uuid4().hex[:8]
    if _es_geodatabase(out_dir):
        return os.path.join(_u(out_dir), u"lcp_backlink_%s" % marca)
    return os.path.join(_u(out_dir), u"lcp_bl_%s.tif" % marca)


# --------------------------------------------------------------------------- #
# Operaciones de documento (necesitan snapshot).
# --------------------------------------------------------------------------- #

# Declaración de codificación (PEP 263). Solo cuenta en las dos primeras líneas.
_CABECERA_CODING = re.compile(u"^[ \\t\\f]*#.*?coding[:=][ \\t]*[-\\w.]+")


def _quitar_cabecera_coding(code):
    """Blanquea la línea `# -*- coding: ... -*-` si está en las dos primeras.

    El código llega como unicode y `exec` de Python 2 PROHÍBE la declaración de
    codificación sobre una cadena unicode ("SyntaxError: encoding declaration in
    Unicode string"), antes de ejecutar una sola línea. Pero quien escribe Python
    2.7 con tildes la pone lo primero, por costumbre, y perdía una llamada cada
    vez. Se deja la línea VACÍA en vez de borrarla para que los números de línea
    del traceback sigan casando con el código enviado.
    """
    lineas = code.split(u"\n")
    for i in range(min(2, len(lineas))):
        if _CABECERA_CODING.match(lineas[i]):
            lineas[i] = u""
    return u"\n".join(lineas)


# `mxd` / `df` usados como VARIABLE (no tras un punto: ahí `.mxd` es una
# extensión de fichero).
_NOMBRA_MXD = re.compile(u"(?<![.\\w])mxd(?!\\w)")
_NOMBRA_DF = re.compile(u"(?<![.\\w])df(?!\\w)")

# stdout que llevaba impreso el código cuando lanzó una excepción. main() lo adjunta
# al sobre de error: sin él, un bucle que falla en el 5º documento no dice que los
# cuatro primeros sí se hicieron.
_STDOUT_AL_FALLAR = None


def op_execute_code(job):
    """Código arcpy arbitrario. Variables: arcpy, MAP/mapping, mxd (SNAPSHOT de la
    sesión), df. Asignar RESULT (se acepta `result` en minúscula, con aviso).

    Sin snapshot en el job, `mxd` y `df` sencillamente no existen: el add-in solo
    copia el documento cuando el código lo necesita, porque copiarlo cuesta
    segundos o minutos en sesiones grandes.
    """
    global _STDOUT_AL_FALLAR, _COPIA_USADA
    code = _quitar_cabecera_coding(job["params"].get("code", u""))
    mxd = _abrir_mxd(job) if job.get("mxd") else None
    buff = _BufferUnicode()
    old_stdout = sys.stdout
    sys.stdout = buff
    ns = {"arcpy": arcpy, "MAP": MAP, "mapping": MAP, "RESULT": None}
    copia_df = None
    if mxd is not None:
        ns["mxd"] = mxd
        # Un documento recién abierto y sin guardar ("Sin título") da una copia
        # en la que `activeDataFrame` no existe: arcpy lanza NameError y el job
        # moría en este preámbulo, antes de la primera línea del usuario, con
        # `ping` en verde. Visto el 2026-09-21 con código que ni siquiera usaba
        # `df` (abría otros .mxd por ruta). Sin data frame, `df` vale None.
        try:
            copia_df = mxd.activeDataFrame
        except Exception:
            copia_df = None
        ns["df"] = copia_df
    _fase(u"ejecutando codigo")
    # `sys.exit()`, `exit()` y Ctrl-C NO son Exception (SystemExit y
    # KeyboardInterrupt cuelgan de BaseException): se escapaban del `except
    # Exception` de main(), el runner moria sin escribir el out.json y el add-in
    # solo podia decir "salida vacia" de un codigo que, en el caso del exit(),
    # habia corrido entero. Se capturan aqui, donde todavia se ve el RESULT.
    interrumpido = None
    try:
        exec(code, ns)
    except SystemExit as ex:
        interrumpido = ex
    except KeyboardInterrupt as ex:
        interrumpido = ex
    except Exception:
        _STDOUT_AL_FALLAR = buff.getvalue()
        raise
    finally:
        sys.stdout = old_stdout
        if mxd is not None:
            # ¿Se usó la copia? Por cada nombre: el código lo menciona como variable
            # Y al terminar sigue apuntando a la copia. Un bucle que hace
            # `mxd = MAP.MapDocument(ruta)` sobre otros documentos lo reasigna, y un
            # `df` que el código ni nombra no cuenta aunque siga intacto.
            _COPIA_USADA = (
                (bool(_NOMBRA_MXD.search(code)) and ns.get("mxd") is mxd)
                or (copia_df is not None and bool(_NOMBRA_DF.search(code))
                    and ns.get("df") is copia_df))
        del mxd
    stdout = buff.getvalue()
    if interrumpido is None:
        resultado = ns.get("RESULT")
        if resultado is None and ns.get("result") is not None:
            # `result = ...` en minúscula es la convención del MCP de ArcGIS Pro, y
            # la confusión entre los dos servidores costaba una llamada cada vez:
            # volvía `result: null` con el valor calculado y tirado. Se devuelve.
            return {"result": ns.get("result"), "stdout": stdout,
                    "aviso_result": u"Asignaste `result` en minúscula; en este servidor la "
                                    u"variable es RESULT. Se ha devuelto igual."}
        return {"result": resultado, "stdout": stdout}

    if isinstance(interrumpido, KeyboardInterrupt):
        raise ValueError(u"El codigo fue interrumpido (KeyboardInterrupt) antes de "
                         u"terminar, asi que no hay RESULT que devolver.%s"
                         % _resumen_stdout(stdout))

    codigo = getattr(interrumpido, "code", None)
    if codigo is None:
        codigo, mensaje = 0, None
    elif isinstance(codigo, int):
        mensaje = None
    else:
        # sys.exit("texto") imprime el texto y sale con codigo 1.
        codigo, mensaje = 1, _u(codigo)
    resultado = ns.get("RESULT")
    if codigo == 0 and resultado is not None:
        # La clave NO puede ser "aviso": el handler ExecuteArcpy del add-in
        # sobrescribe `result.aviso` sin mirar (ahi pone el aviso del snapshot),
        # asi que esta nota no llegaria nunca al llamante.
        return {"result": resultado, "stdout": stdout,
                "aviso_salida": u"El codigo llamo a sys.exit() y se ha devuelto el "
                                u"RESULT que ya tenia asignado. En execute_arcpy no "
                                u"hace falta salir: basta con asignar RESULT."}
    raise ValueError(u"El codigo termino con sys.exit(%s)%s y no dejo RESULT "
                     u"asignado, asi que no hay nada que devolver. En "
                     u"execute_arcpy no se sale con sys.exit: se asigna RESULT.%s"
                     % (codigo, u" -> %s" % mensaje if mensaje else u"",
                        _resumen_stdout(stdout)))


def op_list_ddp(job):
    """Contrato JSON de la tool list_ddp del servidor MCP, sobre el snapshot."""
    params = job["params"]
    mxd = _abrir_mxd(job)
    try:
        try:
            ddp = mxd.dataDrivenPages
        except Exception:
            ddp = None
        if ddp is None:
            return {"habilitado": False}
        campo = ddp.pageNameField.name if ddp.pageNameField else None
        capa_idx = ddp.indexLayer.name if ddp.indexLayer else None
        maximo = int(params.get("max_valores", 500))
        valores = []
        truncado = False
        if campo and ddp.indexLayer is not None:
            try:
                for row in arcpy.da.SearchCursor(ddp.indexLayer, [campo]):
                    if len(valores) >= maximo:
                        truncado = True
                        break
                    valores.append(row[0])
            except Exception:
                valores = []
        return {"habilitado": True,
                "num_paginas": ddp.pageCount,
                "campo_nombre": campo,
                "capa_indice": capa_idx,
                "valores": valores,
                "valores_truncados": truncado}
    finally:
        del mxd


def op_export_ddp(job):
    """Contrato JSON de la tool export_ddp del servidor MCP, sobre el snapshot."""
    params = job["params"]
    mxd = _abrir_mxd(job)
    try:
        ddp = _get_ddp(mxd)
        salida = params["salida"]
        if not salida.lower().endswith(".pdf"):
            salida += ".pdf"
        dpi = int(params.get("dpi", 300))
        modo = (params.get("modo") or "ALL").upper()
        rango = params.get("rango")
        valores = params.get("valores")
        un_por_pagina = bool(params.get("un_pdf_por_pagina", False))

        page_range_string = u""
        no_encontrados = []
        if valores:
            ids = []
            for v in valores:
                pid = ddp.getPageIDFromName(_u(v))
                if pid:
                    ids.append(pid)
                else:
                    # Un valor que no casa con ninguna pagina se descartaba EN
                    # SILENCIO: pedias 12 expedientes, recibias un PDF de 9 y
                    # nada en la respuesta decia cuales faltaban. Un error de
                    # tecleo o un expediente que ya no esta en la capa indice se
                    # leian igual que un exito.
                    no_encontrados.append(_u(v))
            if not ids:
                raise ValueError(u"Ningún valor coincide con páginas del atlas: %s"
                                 % u", ".join(_u(v) for v in valores))
            page_range_string = u",".join(str(i) for i in ids)
            range_type = "RANGE"
        elif rango:
            page_range_string = _u(rango)
            range_type = "RANGE"
        elif modo == "CURRENT":
            range_type = "CURRENT"
        else:
            range_type = "ALL"

        multiple = "PDF_MULTIPLE_FILES_PAGE_NAME" if un_por_pagina else "PDF_SINGLE_FILE"
        ddp.exportToPDF(salida, range_type, page_range_string, multiple, dpi)

        if range_type == "ALL":
            num = ddp.pageCount
        elif range_type == "CURRENT":
            num = 1
        else:
            num = _contar_paginas_rango(page_range_string)
        out = {"salida": salida, "modo_efectivo": range_type,
               "page_range": page_range_string, "num": num,
               "un_pdf_por_pagina": un_por_pagina, "dpi": dpi,
               "valores_no_encontrados": no_encontrados}
        if no_encontrados:
            out["aviso"] = (u"%d de los %d valores pedidos NO existen en la capa "
                            u"índice y no están en el PDF: %s. Revisa list_ddp "
                            u"para ver los valores reales del atlas."
                            % (len(no_encontrados), len(valores),
                               u", ".join(no_encontrados)))
        return out
    finally:
        del mxd


def op_ddp_page_extent(job):
    """goto_ddp_page out-of-process: sitúa el atlas del SNAPSHOT en la página y
    devuelve el extent/escala resultantes para que el add-in los aplique al data
    frame VIVO (aproximación: la sesión viva no cambia de página de atlas)."""
    params = job["params"]
    mxd = _abrir_mxd(job)
    try:
        ddp = _get_ddp(mxd)
        pagina = params.get("pagina")
        valor = params.get("valor")
        if valor is not None:
            pid = ddp.getPageIDFromName(_u(valor))
            if not pid:
                raise ValueError(u"Valor de índice no encontrado en el atlas: %s" % _u(valor))
        elif pagina is not None:
            pid = int(pagina)
        else:
            raise ValueError(u"Indica 'pagina' (ID 1-based) o 'valor' (campo índice).")
        ddp.currentPageID = pid
        try:
            nombre_pagina = ddp.pageRow.getValue(ddp.pageNameField.name)
        except Exception:
            nombre_pagina = None
        df = mxd.activeDataFrame
        ext = df.extent
        return {"page_id": pid, "valor": nombre_pagina, "escala": df.scale,
                "extent": [ext.XMin, ext.YMin, ext.XMax, ext.YMax]}
    finally:
        del mxd


# --------------------------------------------------------------------------- #
# Ambientales (datos en disco; sin snapshot). Solo arcpy: el añadir-al-mapa lo hace el add-in.
# --------------------------------------------------------------------------- #

_INDICE_BANDAS = {
    "NDVI":  ("NIR", "RED"),
    "GNDVI": ("NIR", "GREEN"),
    "NDRE":  ("NIR", "REDEDGE"),
    "NDWI":  ("GREEN", "NIR"),
    "MNDWI": ("GREEN", "SWIR1"),
    "NDMI":  ("NIR", "SWIR1"),
    "NBR":   ("NIR", "SWIR2"),
    "SAVI":  ("NIR", "RED"),
    "EVI":   ("NIR", "RED", "BLUE"),
}


def op_raster_index(job):
    params = job["params"]
    indice = (params.get("indice") or u"").upper()
    bandas = params.get("bandas") or {}
    salida = params.get("salida")
    banda_a = params.get("banda_a")
    banda_b = params.get("banda_b")
    if not salida:
        raise ValueError(u"Indica 'salida' (ruta del ráster de índice).")
    _preparar_salidas(params, [salida])
    _checkout("Spatial")
    try:
        from arcpy.sa import Raster, Float

        def R(rol):
            ruta = bandas.get(rol)
            if not ruta:
                req = _INDICE_BANDAS.get(indice, ())
                raise ValueError(u"Falta la banda '%s' para %s (requiere: %s). "
                                 u"Pásala en 'bandas', ej. {'NIR': ruta, 'RED': ruta}."
                                 % (_u(rol), _u(indice), u", ".join(req)))
            return Float(Raster(_u(ruta)))

        if indice == "NDVI":
            nir, red = R("NIR"), R("RED"); idx = (nir - red) / (nir + red)
        elif indice == "GNDVI":
            nir, grn = R("NIR"), R("GREEN"); idx = (nir - grn) / (nir + grn)
        elif indice == "NDRE":
            nir, re = R("NIR"), R("REDEDGE"); idx = (nir - re) / (nir + re)
        elif indice == "NDWI":
            grn, nir = R("GREEN"), R("NIR"); idx = (grn - nir) / (grn + nir)
        elif indice == "MNDWI":
            grn, s1 = R("GREEN"), R("SWIR1"); idx = (grn - s1) / (grn + s1)
        elif indice == "NDMI":
            nir, s1 = R("NIR"), R("SWIR1"); idx = (nir - s1) / (nir + s1)
        elif indice == "NBR":
            nir, s2 = R("NIR"), R("SWIR2"); idx = (nir - s2) / (nir + s2)
        elif indice == "SAVI":
            L = float(params.get("L", 0.5))
            nir, red = R("NIR"), R("RED")
            idx = ((nir - red) / (nir + red + L)) * (1 + L)
        elif indice == "EVI":
            nir, red, blue = R("NIR"), R("RED"), R("BLUE")
            idx = 2.5 * ((nir - red) / (nir + 6 * red - 7.5 * blue + 1))
        elif indice in (u"", u"CUSTOM"):
            if not (banda_a and banda_b):
                raise ValueError(u"Índice genérico: indica banda_a y banda_b "
                                 u"(o usa 'indice' con un nombre conocido).")
            a = Float(Raster(_u(banda_a))); b = Float(Raster(_u(banda_b)))
            idx = (a - b) / (a + b)
            indice = u"CUSTOM"
        else:
            raise ValueError(u"Índice no soportado: %s. Disponibles: %s, CUSTOM."
                             % (_u(indice), u", ".join(sorted(_INDICE_BANDAS))))
        idx.save(_u(salida))
    finally:
        arcpy.CheckInExtension("Spatial")
    return {"indice": indice, "salida": salida}


def op_hydrology(job):
    params = job["params"]
    op = (params.get("operacion") or u"").lower()
    p = params.get("parametros") or {}
    # Las salidas se conocen ANTES de tocar la licencia: el aviso de "ya existe"
    # no debe costar un checkout de Spatial Analyst. `cuenca` escribe ademas fdir
    # y facc con nombre fijo en salida_dir, y esos chocan igual en la 2ª pasada.
    salidas = [p.get("salida")]
    if op == "cuenca" and p.get("salida_dir"):
        salidas.append(os.path.join(_u(p["salida_dir"]), "fdir"))
        salidas.append(os.path.join(_u(p["salida_dir"]), "facc"))
    _preparar_salidas(params, salidas)
    _checkout("Spatial")
    try:
        from arcpy.sa import (Raster, Fill, FlowDirection, FlowAccumulation,
                              Watershed, Basin, Con, StreamToFeature, SnapPourPoint)
        res = {"operacion": op}
        if op == "cuenca":
            out_dir = p["salida_dir"]
            if not os.path.isdir(out_dir):
                os.makedirs(out_dir)
            relleno = Fill(Raster(_u(p["mdt"])))
            fdir = FlowDirection(relleno)
            facc = FlowAccumulation(fdir)
            fdir.save(os.path.join(_u(out_dir), "fdir"))
            facc.save(os.path.join(_u(out_dir), "facc"))
            if p.get("pour_points"):
                pp = _u(p["pour_points"])
                if p.get("snap_dist"):
                    pp = SnapPourPoint(pp, facc, float(p["snap_dist"]))
                cuencas = Watershed(fdir, pp)
            else:
                cuencas = Basin(fdir)
            cuencas.save(_u(p["salida"]))
            res["salida"] = p["salida"]
            res["fdir"] = os.path.join(_u(out_dir), "fdir")
            res["facc"] = os.path.join(_u(out_dir), "facc")
        elif op == "red_drenaje":
            if p.get("fdir") and p.get("facc"):
                fdir = Raster(_u(p["fdir"]))
                facc = Raster(_u(p["facc"]))
            else:
                fdir = FlowDirection(Fill(Raster(_u(p["mdt"]))))
                facc = FlowAccumulation(fdir)
            rios = Con(facc > float(p["umbral"]), 1)
            StreamToFeature(rios, fdir, _u(p["salida"]), "NO_SIMPLIFY")
            res["salida"] = p["salida"]
        elif op == "inundacion":
            agua = Con(Raster(_u(p["mdt"])) <= float(p["nivel"]), 1)
            agua.save(_u(p["salida"]))
            res["salida"] = p["salida"]
        else:
            raise ValueError(u"Operación hidrológica no soportada: %s "
                             u"(usa cuenca | red_drenaje | inundacion)." % _u(op))
        res["mensajes"] = arcpy.GetMessages()
    finally:
        arcpy.CheckInExtension("Spatial")
    return res


def op_contours(job):
    params = job["params"]
    mdt = params.get("mdt")
    salida = params.get("salida")
    intervalo = params.get("intervalo")
    base = params.get("base", 0)
    dxf = params.get("dxf")
    if not (mdt and salida and intervalo is not None):
        raise ValueError(u"Indica 'mdt', 'salida' e 'intervalo' (equidistancia).")
    _preparar_salidas(params, [salida, dxf])
    _checkout("3D")
    try:
        arcpy.ddd.Contour(_u(mdt), _u(salida), float(intervalo), float(base))
        out = {"salida": salida, "intervalo": intervalo, "base": base}
        if dxf:
            arcpy.conversion.ExportCAD([_u(salida)], "DXF_R2010", _u(dxf))
            out["dxf"] = dxf
    finally:
        arcpy.CheckInExtension("3D")
    return out


def op_topographic_profile(job):
    params = job["params"]
    superficie = params.get("superficie")
    lineas = params.get("lineas")
    salida = params.get("salida")
    if not (superficie and lineas and salida):
        raise ValueError(u"Indica 'superficie' (ráster/TIN), 'lineas' (2D) y 'salida'.")
    _preparar_salidas(params, [salida])
    _checkout("3D")
    try:
        arcpy.ddd.InterpolateShape(_u(superficie), _u(lineas), _u(salida))
    finally:
        arcpy.CheckInExtension("3D")
    return {"salida": salida, "superficie": superficie, "lineas": lineas}


def op_least_cost_path(job):
    params = job["params"]
    coste = params.get("coste")
    origen = params.get("origen")
    destino = params.get("destino")
    salida = params.get("salida")
    salida_dir = params.get("salida_dir")
    if not (coste and origen and destino and salida):
        raise ValueError(u"Indica 'coste' (ráster de fricción), 'origen', 'destino' y 'salida'.")
    _preparar_salidas(params, [salida])
    _checkout("Spatial")
    try:
        from arcpy.sa import CostDistance, CostPath, Raster
        out_dir = salida_dir or os.path.dirname(_u(salida)) or arcpy.env.scratchFolder
        backlink = _ruta_backlink(out_dir)
        cdist = CostDistance(_u(origen), _u(coste), out_backlink_raster=backlink)
        lcp = CostPath(_u(destino), cdist, Raster(backlink), "EACH_CELL")
        lcp.save(_u(salida))
        return {"salida": salida, "backlink": backlink}
    finally:
        arcpy.CheckInExtension("Spatial")


def _tool_arcpy(tool):
    """'management.GetCount' o 'GetCount_management' -> (funcion, nombre, alias).

    Por arcpy.gp.<Tool>_<alias>, NO por arcpy.<modulo>.<Tool>: en Spatial Analyst el
    modulo es algebra de mapas, con otra firma (arcpy.sa.Slope(in_raster, ...) devuelve
    un Raster y no admite ruta de salida), mientras que gp tiene la del geoprocesador,
    la misma que la via nativa y la que se comprueba con Usage en el add-in.
    Error accionable si no existe: la llamada nativa dice lo mismo."""
    tool = _u(tool or u"").strip()
    if u"." in tool:
        modulo, nombre = tool.split(u".", 1)
    elif u"_" in tool:
        nombre, modulo = tool.rsplit(u"_", 1)
    else:
        raise ValueError(u"Indica 'tool' como 'modulo.Herramienta' (p. ej. "
                         u"'management.GetCount') o 'Herramienta_modulo'.")
    alias = u"3d" if modulo.lower() == u"ddd" else modulo.lower()
    try:
        fn = getattr(arcpy.gp, str(nombre + u"_" + alias))
    except (AttributeError, UnicodeEncodeError):
        fn = None
    if fn is None:
        raise ValueError(u"No existe la herramienta de geoproceso '%s'. Usa la forma "
                         u"de arcpy, 'modulo.Herramienta' (p. ej. 'management.GetCount')."
                         % tool)
    return fn, nombre, alias


def _capas_salida(resultado):
    """Salidas del Result que son datos que se pueden ver (feature class, raster,
    shapefile). Un derivado como el recuento de GetCount ('24') no lo es."""
    rutas = []
    for i in range(resultado.outputCount):
        try:
            valor = _u(resultado.getOutput(i))
        except Exception:
            continue
        if not valor or not _existe(valor):
            continue
        try:
            tipo = arcpy.Describe(valor).dataType
        except Exception:
            continue
        if tipo in (u"FeatureClass", u"ShapeFile", u"RasterDataset", u"RasterBand"):
            rutas.append(valor)
    return rutas


def op_geoprocessing(job):
    """run_geoprocessing(fuera_de_arcmap=True): el geoproceso en este proceso, no en
    el hilo de ArcMap, que se queda libre mientras corre. Los parametros llegan ya
    como rutas: el add-in traduce las capas de la TOC antes de lanzar el job."""
    params = job["params"]
    tool = params.get("tool")
    fn, nombre, alias = _tool_arcpy(tool)
    args = params.get("params") or []
    # Por defecto NO se sobrescribe: el geoprocesador rechaza una salida existente al
    # validar, antes de calcular (ERROR 000725), y con True una ruta mal tecleada
    # borraria el resultado de ayer sin decir nada.
    arcpy.env.overwriteOutput = bool(params.get("sobrescribir", False))
    ext = {u"sa": "Spatial", u"3d": "3D"}.get(alias)
    if ext:
        _checkout(ext)
    try:
        try:
            resultado = fn(*args)
        except TypeError as exc:
            # Red: los parametros de mas los rechaza ANTES el add-in con Usage, porque por
            # arcpy.gp uno de mas se ignora y dos tumban este proceso (2026-09-26).
            try:
                firma = _u(arcpy.Usage(str(nombre + u"_" + alias)))
            except Exception:
                firma = u"(no disponible)"
            raise ValueError(u"Parametros no validos para '%s': %s. Firma: %s"
                             % (_u(tool), _u(exc), firma))
        except arcpy.ExecuteError:
            mensajes = _u(arcpy.GetMessages(2) or arcpy.GetMessages())
            pista = u""
            if u"000258" in mensajes and params.get("sobrescribir"):
                # Medido 2026-09-26: ArcMap bloquea un dato que se ha cargado en la
                # sesion y NO lo suelta al quitar la capa (seguia bloqueado 15 s despues
                # de remove_layer, y forzar el recolector de .NET no lo cambiaba). Desde
                # este proceso no se puede borrar hasta cerrar ArcMap.
                pista = (u" Con sobrescribir=true esto pasa cuando la salida está o ha estado"
                         u" cargada en esta sesión de ArcMap, que la mantiene bloqueada aunque se"
                         u" quite del mapa. Usa otra ruta.")
            raise ValueError(u"Geoproceso '%s' falló. Mensajes GP: %s%s"
                             % (_u(tool), mensajes, pista))
        salidas = []
        for i in range(resultado.outputCount):
            try:
                salidas.append(_u(resultado.getOutput(i)))
            except Exception:
                salidas.append(None)
        return {"tool": tool, "salidas": salidas,
                "mensajes": _u(resultado.getMessages()),
                "capas_salida": _capas_salida(resultado)}
    finally:
        if ext:
            arcpy.CheckInExtension(ext)


# calculate_geometry NO vive aquí: la sesión viva mantiene un schema lock sobre
# las fuentes cargadas en la TOC y AddGeometryAttributes añade campos → imposible
# out-of-process. Va nativo por IGeoProcessor2 dentro del add-in.

OPS = {
    "execute_code": op_execute_code,
    "list_ddp": op_list_ddp,
    "export_ddp": op_export_ddp,
    "ddp_page_extent": op_ddp_page_extent,
    "raster_index": op_raster_index,
    "hydrology": op_hydrology,
    "contours": op_contours,
    "topographic_profile": op_topographic_profile,
    "least_cost_path": op_least_cost_path,
    "geoprocessing": op_geoprocessing,
}


def _json_default(o):
    """RESULT puede traer objetos arcpy no serializables: degradar a texto."""
    try:
        return unicode(o)
    except Exception:
        return repr(o)


def _clave(k):
    """Clave de diccionario como texto. json.dumps de Py2 revienta con
    "keys must be a string" ante una tupla o un objeto; degradarla a texto es
    peor que tenerla bien, pero infinitamente mejor que perder el job entero."""
    if isinstance(k, unicode):
        return k
    if isinstance(k, str):
        return _u(k)
    try:
        return unicode(k)
    except Exception:
        return _u(repr(k))


def _sanear(valor):
    """Todo `str` a unicode, recursivamente, ANTES de json.dumps.

    Con ensure_ascii=False, json.dumps decodifica cada `str` que encuentra como
    UTF-8, y en Windows hay `str` que NO son UTF-8: `os.listdir()` sobre una
    ruta `str` devuelve los nombres en la codepage ANSI, asi que un RESULT con
    "Cartografia" acentuada reventaba con UnicodeDecodeError y se perdia el
    trabajo entero por el nombre de un fichero. `_u` prueba utf-8 y cae a
    latin-1, que nunca falla.

    Solo se tocan `str` y contenedores: numeros, booleanos y None pasan
    intactos (coaccionarlos convertiria un 42 en "42" y mentiria sobre el tipo
    del resultado). Lo que no es ninguna de esas cosas lo sigue resolviendo
    `_json_default` al serializar.
    """
    if isinstance(valor, str):
        return _u(valor)
    if isinstance(valor, dict):
        return dict((_clave(k), _sanear(v)) for k, v in valor.items())
    if isinstance(valor, (list, tuple)):
        return [_sanear(v) for v in valor]
    return valor


def _serializar(respuesta):
    """JSON como UNICODE, siempre.

    Con ensure_ascii=False, json.dumps de Py2 devuelve `str` (bytes) cuando el
    payload es ASCII puro y `unicode` en cuanto aparece un no-ASCII. Escribir ese
    `str` en un fichero abierto con io.open(encoding=...) lanza
    "write() argument 1 must be unicode" → el fichero de salida quedaba VACÍO y el
    add-in fallaba con "Error reading JObject ... line 0, position 0". La coerción
    explícita es obligatoria: no la quites.

    El saneo previo es de la misma familia: json.dumps no sabe que hacer con un
    `str` que no sea UTF-8, y arcpy y os devuelven unos cuantos.
    """
    texto = json.dumps(_sanear(respuesta), ensure_ascii=False, default=_json_default)
    if isinstance(texto, str):
        texto = texto.decode("utf-8")
    return texto


def _escribir_salida(out_path, respuesta):
    """Escritura ATÓMICA: serializa entero, escribe a .tmp y renombra.

    El add-in solo ve el fichero cuando está completo, así que un fallo a mitad
    nunca puede presentarse como 'salida vacía'.
    """
    texto = _serializar(respuesta)
    tmp = out_path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(texto)
    if os.path.exists(out_path):
        os.remove(out_path)
    os.rename(tmp, out_path)


def main():
    global _MXD_ORIGINAL
    if len(sys.argv) != 3:
        sys.stderr.write("uso: runner.py <job.json> <out.json>\n")
        return 2
    job_path, out_path = sys.argv[1], sys.argv[2]
    try:
        _fase(u"leyendo job")
        with io.open(job_path, "r", encoding="utf-8") as f:
            job = json.loads(f.read())
        op = OPS.get(job.get("op"))
        if op is None:
            raise ValueError(u"Operación desconocida: %s" % _u(job.get("op")))
        _MXD_ORIGINAL = job.get("mxd_original")
        _fase(u"ejecutando op", _u(job.get("op")))
        respuesta = {"ok": True, "result": _adjuntar_rotas(op(job))}
    except Exception as ex:
        respuesta = {"ok": False,
                     "error": _u(ex.message if getattr(ex, "message", None) else ex),
                     "traceback": _u(traceback.format_exc())}
        if _STDOUT_AL_FALLAR:
            # Lo que el código ya había impreso: en un bucle, es lo único que dice
            # por dónde iba y qué se llegó a hacer antes del fallo.
            respuesta["stdout"] = _STDOUT_AL_FALLAR
        if isinstance(ex, UnicodeError):
            respuesta["pista"] = (
                u"Error de codificación dentro de tu código, no del runner (la salida "
                u"de print ya se captura como unicode). La causa típica es convertir a "
                u"`str` un texto con tildes: `str(ex)`, `\"%s\" % ex` o `print ex` sobre "
                u"una excepción de arcpy. Usa `unicode(ex)` o `ex.message`, y literales u\"...\".")
    try:
        _fase(u"serializando salida")
        _escribir_salida(out_path, respuesta)
        _fase(u"terminado")
    except Exception:
        # Último recurso: el resultado no era serializable o el disco falló. Se
        # responde un sobre de error válido (ASCII puro) en vez de dejar el
        # fichero vacío, que es lo que el add-in no sabe interpretar.
        fallback = {"ok": False,
                    "error": u"El runner no pudo serializar la respuesta de la operación.",
                    "traceback": _u(traceback.format_exc())}
        try:
            _escribir_salida(out_path, fallback)
        except Exception:
            with io.open(out_path, "w", encoding="utf-8") as f:
                f.write(u'{"ok": false, "error": "El runner no pudo escribir su salida."}')
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
