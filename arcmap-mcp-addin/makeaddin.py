# -*- coding: utf-8 -*-
"""
makeaddin.py  ──  Empaqueta esta carpeta en un fichero .esriaddin instalable.

Uso (con cualquier Python 2.7/3, p.ej. el de ArcMap):
    python makeaddin.py

Genera 'arcmap-mcp-addin.esriaddin' en esta misma carpeta. Haz doble clic en él
para instalar el Add-In en ArcMap (Esri ESRI Add-In Installation Utility).
Luego en ArcMap: Customize > Toolbars > arcmap-mcp.
"""
import os
import re
import zipfile

current_path = os.path.dirname(os.path.abspath(__file__))
out_zip_name = os.path.join(current_path,
                            os.path.basename(current_path) + ".esriaddin")

BACKUP_FILE_PATTERN = re.compile(r".*_addin_[0-9]+[.]py$", re.IGNORECASE)


def looks_like_a_backup(filename):
    return bool(BACKUP_FILE_PATTERN.match(filename))


zip_file = zipfile.ZipFile(out_zip_name, "w", zipfile.ZIP_DEFLATED)
try:
    for root, dirs, files in os.walk(current_path):
        # No empaquetar cache de bytecode (p.ej. al validar con py3).
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
        for filename in files:
            if filename.endswith(".esriaddin"):
                continue
            if filename.endswith(".pyc"):
                continue
            if looks_like_a_backup(filename):
                continue
            abs_path = os.path.join(root, filename)
            arc_path = os.path.relpath(abs_path, current_path)
            zip_file.write(abs_path, arc_path)
finally:
    zip_file.close()

print("Add-In creado en: " + out_zip_name)
