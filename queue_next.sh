#!/usr/bin/env bash
# Espera a que termine la traducción actual (Slow Looking) y arranca la siguiente (The Score).
# Auto-encadena: cambia el perfil, respalda progress.json, lanza el script y mantiene caffeinate.

set -euo pipefail

PROJECT="/Users/stivenkevinrosalescasas/Downloads/Traducción epub"
cd "$PROJECT"

PREV_PID="${1:-}"
if [[ -z "$PREV_PID" ]]; then
  echo "uso: $0 <pid_traduccion_actual>"
  exit 1
fi

echo "[queue] esperando que termine PID $PREV_PID (Slow Looking)…"
while kill -0 "$PREV_PID" 2>/dev/null; do
  sleep 30
done
echo "[queue] PID $PREV_PID terminó. Pausa de 5s antes de continuar…"
sleep 5

# Verificar que SlowLooking_es.epub existe (señal de éxito)
if [[ -f "SlowLooking_es.epub" ]]; then
  echo "[queue] ✓ SlowLooking_es.epub generado"
else
  echo "[queue] ⚠ SlowLooking_es.epub NO encontrado — la traducción anterior pudo fallar"
  echo "[queue] continúo de todas formas, pero revisa translate_slow_looking.log"
fi

# Respaldar progress.json del libro anterior
if [[ -f "progress.json" ]]; then
  mv -v progress.json progress_slow_looking_done.json
fi

# Limpiar work/ (será re-extraído por el script con el nuevo libro)
rm -rf work

# Cambiar FORCE_PROFILE de slow_looking a the_score
sd 'FORCE_PROFILE: str \| None = "slow_looking"' \
   'FORCE_PROFILE: str | None = "the_score"' \
   translate_epub.py
echo "[queue] FORCE_PROFILE → the_score"

# Tocar TheScore.epub para que sea el más reciente y aparezca como índice 0 en el menú
touch TheScore.epub

# Lanzar la traducción de The Score
echo "[queue] lanzando traducción de The Score…"
source .venv/bin/activate
(echo "0" | python3 -u translate_epub.py) > translate_the_score.log 2>&1 &
NEW_PID=$!
disown
echo "[queue] traducción arrancada con PID $NEW_PID"

# Re-vincular caffeinate al nuevo proceso
caffeinate -i -s -w "$NEW_PID" > /dev/null 2>&1 &
disown
echo "[queue] caffeinate re-vinculado al nuevo PID"

echo "[queue] hecho. Log: translate_the_score.log"
