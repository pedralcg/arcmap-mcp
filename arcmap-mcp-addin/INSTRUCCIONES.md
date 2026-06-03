# Add-In arcmap-mcp — botón dentro de ArcMap 10.5

Barra de herramientas con cuatro botones: **Iniciar**, **Detener**, **Estado**,
**Acerca de**. Arranca/para el puente socket (127.0.0.1:27179) sin pegar nada en la consola.

## Instalar (una vez)

1. Doble clic en **`arcmap-mcp-addin.esriaddin`** → se abre el *ESRI Add-In
   Installation Utility* → **Install**.
2. Abre ArcMap 10.5. Si no ves la barra: **Customize ▸ Toolbars ▸ arcmap-mcp**
   (o **Customize ▸ Add-In Manager** para confirmar que está cargado).

> El Add-In importa `arcmap_bridge.py` desde **`C:\mcp\arcmap-mcp\src`** por defecto.
> Si el repo vive en otra ruta, define la variable de entorno **`ARCMAP_MCP_DIR`**
> apuntando a la carpeta del repo **antes de abrir ArcMap** (no hace falta reconstruir
> el Add-In). Alternativa: editar `REPO_DIR` en `Install\arcmap_mcp_addin.py` y reconstruir.

## Usar

- **Iniciar** → puente escuchando. Aparece un aviso con el host:puerto.
- **Estado** → dice si está ACTIVO o parado.
- **Detener** → lo detiene.
- **Acerca de** → ficha del autor (pedralcg.dev).

Con el puente activo, lanza el túnel/servidor desde fuera:
`..\start-arcmap-mcp.ps1` (o deja que Claude Code arranque el servidor MCP).

## Reconstruir el paquete (tras editar el Add-In)

```powershell
python makeaddin.py        # regenera arcmap-mcp-addin.esriaddin
```

Reinstalar tiene dos vías:

- **Doble clic ▸ Install** (ESRI Add-In Installation Utility) y reiniciar ArcMap.
- **Sustitución directa** (más fiable cuando ArcMap cachea la versión vieja con el
  mismo GUID): con ArcMap **cerrado**, copiar el nuevo `.esriaddin` sobre el
  instalado en
  `…\Documents\ArcGIS\AddIns\Desktop10.5\{931c022e-51ae-4daf-84ff-c1015b130649}\arcmap-mcp-addin.esriaddin`.
  ArcMap relee ese paquete en cada arranque.

> Sube `<Version>` en `config.xml` al editar, para distinguir la build instalada.

## Gotcha: botones marcados como "Falta" / "Missing"

Si el Add-In Manager muestra los tres botones como **Falta**, casi siempre es que
el atributo `class=` de cada `<Button>` en `config.xml` **no lleva el prefijo del
módulo**. ArcMap exige `class="<library_sin_.py>.<NombreClase>"`, igual que lo
genera el Add-In Wizard de Esri. Con `library="arcmap_mcp_addin.py"` debe ser
`class="arcmap_mcp_addin.ButtonStart"` (no `class="ButtonStart"`). Sin el prefijo
ArcMap no resuelve la clase Python y marca el componente como ausente. *(Corregido
en v0.2.)*

Otras causas que dan el mismo síntoma: error de sintaxis o `import` que falla a
nivel de módulo en `Install\arcmap_mcp_addin.py` (rompe la carga de todas las
clases), o `library=` que no coincide con el nombre real del `.py`.

## Nota técnica

El botón solo cambia la **forma de arrancar** el puente (frente a `execfile`); la
mecánica de servicio del socket ya está resuelta: el puente sondea el socket con
`user32.SetTimer` en el **hilo principal** de ArcMap (no en un hilo de fondo, que
en el intérprete embebido de 10.5 no atiende conexiones). Así `arcpy` y
`MapDocument("CURRENT")` se ejecutan en el hilo correcto. Detalle completo en la
sección *Arquitectura* del README.
