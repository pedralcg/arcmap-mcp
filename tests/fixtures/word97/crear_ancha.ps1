param([string]$Destino)
$w = New-Object -ComObject Word.Application
$w.Visible = $false; $w.DisplayAlerts = 0
try {
    $d = $w.Documents.Add()
    $t = $d.Tables.Add($d.Content, 3, 40)
    for ($j = 1; $j -le 40; $j++) { if ($j % 3 -ne 0) { $t.Cell(1, $j).Range.Text = "c$j" } }
    $t.Cell(2, 1).Range.Text = "fila2"
    $t.Cell(3, 40).Range.Text = "ultima"
    $d.RemovePersonalInformation = $true; $d.SaveAs([ref](Join-Path $Destino "t6_ancha.doc"), [ref]0); $d.Close([ref]0)
} finally { $w.Quit([ref]0) }
