# Crea .doc (Word 97-2003) con tablas de celdas vacías para probar _filas_de_tabla_word.
param([string]$Destino)
$w = New-Object -ComObject Word.Application
$w.Visible = $false
$w.DisplayAlerts = 0

function Tabla($doc, $filas) {
    $r = $doc.Content
    $r.Collapse(0)
    $nf = $filas.Count
    $nc = ($filas | ForEach-Object { $_.Count } | Measure-Object -Maximum).Maximum
    $t = $doc.Tables.Add($r, $nf, $nc)
    for ($i = 0; $i -lt $nf; $i++) {
        for ($j = 0; $j -lt $filas[$i].Count; $j++) {
            $t.Cell($i + 1, $j + 1).Range.Text = $filas[$i][$j]
        }
    }
    return $t
}

function Guardar($doc, $nombre) {
    $doc.RemovePersonalInformation = $true
    $doc.SaveAs([ref](Join-Path $Destino $nombre), [ref]0)
    $doc.Close([ref]0)
}

try {
    # 1: el caso del revisor, filas [A, ""] y [C, D]
    $d = $w.Documents.Add(); Tabla $d @(@("A", ""), @("C", "D")) | Out-Null; Guardar $d "t1_vacia_final.doc"

    # 2: vacías en todas las posiciones, con una fila entera vacía
    $d = $w.Documents.Add(); Tabla $d @(@("", "", "X"), @("", "", ""), @("B", "", ""), @("", "E", "")) | Out-Null
    Guardar $d "t2_huecos.doc"

    # 3: una sola columna con una vacía en medio
    $d = $w.Documents.Add(); Tabla $d @(@("x"), @(""), @("z")) | Out-Null; Guardar $d "t3_una_columna.doc"

    # 4: texto antes, tabla con celda de dos párrafos, texto entre medias, segunda tabla, texto después
    $d = $w.Documents.Add()
    $d.Content.Text = "Titulo del cuadro"
    $d.Content.InsertParagraphAfter()
    $t = Tabla $d @(@("COL A", "COL B"), @("uno", ""))
    $t.Cell(2, 1).Range.Text = "linea 1`rlinea 2"
    $r = $d.Content; $r.Collapse(0); $r.InsertAfter("Entre tablas"); $r.InsertParagraphAfter()
    Tabla $d @(@("", "q"), @("r", "")) | Out-Null
    $r = $d.Content; $r.Collapse(0); $r.InsertAfter("Nota final")
    Guardar $d "t4_texto_y_dos_tablas.doc"

    # 5: primera fila combinada (1 celda), segunda de 3 con la del medio vacía
    $d = $w.Documents.Add()
    $t = Tabla $d @(@("", "", ""), @("p", "", "s"))
    $t.Cell(1, 1).Merge($t.Cell(1, 3))
    $t.Cell(1, 1).Range.Text = "CABECERA"
    Guardar $d "t5_combinada.doc"
}
finally {
    $w.Quit([ref]0)
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($w) | Out-Null
}
