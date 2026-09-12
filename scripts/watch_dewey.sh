#!/usr/bin/env bash
# watch_dewey.sh — vista EN VIVO del progreso de la traducción de Dewey.
# Uso: ./watch_dewey.sh   (Ctrl+C para salir)
cd "$(dirname "$0")/.." || exit 1
LOG="logs/dewey_run.log"
OUT="libros/traducidos/Art as Experience - John Dewey_es.epub"

while true; do
  clear
  echo "═══════════════════════════════════════════════"
  echo "  Traducción Dewey · $(date '+%H:%M:%S')"
  echo "═══════════════════════════════════════════════"

  if pgrep -f "translate_epub.py" >/dev/null; then
    echo "Estado : ▶ CORRIENDO (PID $(pgrep -f translate_epub.py | head -1))"
  else
    echo "Estado : ⏹ proceso NO activo"
  fi

  # archivos completados según progress.json
  if [ -f progress.json ]; then
    done=$(grep -o '"done"' progress.json | wc -l | tr -d ' ')
    echo "Hechos : $done archivos marcados 'done'"
  fi

  echo "-----------------------------------------------"
  # última línea de la barra (tqdm usa \r → lo paso a \n) + avisos
  if [ -f "$LOG" ]; then
    tr '\r' '\n' < "$LOG" | grep -E "Batches:|✓|⚠|Error|Traceback" | tail -4
  else
    echo "(log aún no generado)"
  fi

  if [ -f "$OUT" ]; then
    echo ""
    echo "✅ ¡LISTO! Generado: $OUT"
    break
  fi
  sleep 3
done
