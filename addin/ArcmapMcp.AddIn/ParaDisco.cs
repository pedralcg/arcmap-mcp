// El ejecutable del modo disco (ArcmapMcp.Disco, ADR-010) reutiliza los handlers del
// add-in, que son internal: así hay una sola implementación de cada operación, en vivo
// y en disco.
[assembly: System.Runtime.CompilerServices.InternalsVisibleTo("arcmap-mcp-disco")]
