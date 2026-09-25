using System;
using System.Diagnostics;
using System.Net;
using System.Runtime.InteropServices;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Quién tiene el puerto del puente cuando no se puede abrir.
    ///
    /// El log solo decía "Solo se permite un uso de cada dirección de socket" y el
    /// consejo nombraba un `&lt;PID&gt;` que había que buscar a mano: en la revisión del
    /// 2026-09-25 había 111 fallos así en uso real y ninguna forma de saber si era
    /// un segundo ArcMap legítimo o uno zombi. `GetExtendedTcpTable` da el PID dueño
    /// de cada socket en escucha sin privilegios de administrador, que es justo lo
    /// que `netstat -ano` enseña, sin lanzar un proceso en el arranque de ArcMap.
    /// </summary>
    internal static class PuertoOcupado
    {
        internal sealed class Dueno
        {
            public int Pid;
            public string Proceso;   // null si ya no existe
            public string Ventana;   // título de la ventana principal; "" = sin ventana
            public bool EsEsteProceso;

            public bool PareceZombi
            {
                get { return Proceso != null && !EsEsteProceso && string.IsNullOrEmpty(Ventana); }
            }

            public string Describir()
            {
                if (Proceso == null)
                    return "PID " + Pid + " (el proceso ya no existe)";
                string s = "PID " + Pid + " (" + Proceso + ".exe";
                s += string.IsNullOrEmpty(Ventana) ? ", SIN ventana principal" : ", ventana «" + Ventana + "»";
                if (EsEsteProceso)
                    s += ", este mismo proceso";
                return s + ")";
            }
        }

        /// <summary>Dueño del puerto TCP en escucha, o null si no se encuentra o si la
        /// consulta falla. Nunca lanza: se llama desde el arranque de ArcMap.</summary>
        public static Dueno Buscar(int puerto)
        {
            try
            {
                int pid = PidEnEscucha(puerto);
                if (pid <= 0)
                    return null;
                var d = new Dueno { Pid = pid, EsEsteProceso = pid == Process.GetCurrentProcess().Id };
                try
                {
                    using (Process p = Process.GetProcessById(pid))
                    {
                        d.Proceso = p.ProcessName;
                        d.Ventana = p.MainWindowTitle ?? "";
                    }
                }
                catch (ArgumentException) { /* murió entre la tabla y la consulta */ }
                return d;
            }
            catch (Exception ex)
            {
                Log.Info("No se pudo averiguar quién tiene el puerto " + puerto + ": " + ex.Message);
                return null;
            }
        }

        private const int AF_INET = 2;
        private const int TCP_TABLE_OWNER_PID_LISTENER = 3;

        [StructLayout(LayoutKind.Sequential)]
        private struct MIB_TCPROW_OWNER_PID
        {
            public uint state;
            public uint localAddr;
            public uint localPort;   // big-endian en los 16 bits bajos
            public uint remoteAddr;
            public uint remotePort;
            public uint owningPid;
        }

        [DllImport("iphlpapi.dll", SetLastError = true)]
        private static extern uint GetExtendedTcpTable(IntPtr pTcpTable, ref int dwOutBufLen, bool sort,
                                                       int ipVersion, int tblClass, uint reserved);

        private static int PidEnEscucha(int puerto)
        {
            int tam = 0;
            GetExtendedTcpTable(IntPtr.Zero, ref tam, false, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0);
            IntPtr buf = Marshal.AllocHGlobal(tam);
            try
            {
                uint r = GetExtendedTcpTable(buf, ref tam, false, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0);
                if (r != 0)
                    return -1;
                int filas = Marshal.ReadInt32(buf);
                IntPtr fila = IntPtr.Add(buf, 4);
                int tamFila = Marshal.SizeOf(typeof(MIB_TCPROW_OWNER_PID));
                for (int i = 0; i < filas; i++)
                {
                    var row = (MIB_TCPROW_OWNER_PID)Marshal.PtrToStructure(fila, typeof(MIB_TCPROW_OWNER_PID));
                    int p = IPAddress.NetworkToHostOrder((short)(row.localPort & 0xFFFF)) & 0xFFFF;
                    if (p == puerto)
                        return (int)row.owningPid;
                    fila = IntPtr.Add(fila, tamFila);
                }
                return -1;
            }
            finally
            {
                Marshal.FreeHGlobal(buf);
            }
        }
    }
}
