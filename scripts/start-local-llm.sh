#!/usr/bin/env bash
# Launch the local Qwen3.6-35B-A3B model via llama.cpp (Vulkan) as an OpenAI-compatible server.
# Override with env vars: AXIOM_LLAMA_DIR, AXIOM_MODEL, AXIOM_LLM_PORT, AXIOM_LLM_CTX, AXIOM_LLM_PARALLEL
set -euo pipefail
LLAMA_DIR="${AXIOM_LLAMA_DIR:-$HOME/.local/opt/llama.cpp-b12490/llama-b12490}"
MODEL="${AXIOM_MODEL:-$HOME/.local/share/llm/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf}"
PORT="${AXIOM_LLM_PORT:-8081}"
CTX="${AXIOM_LLM_CTX:-32768}"
PAR="${AXIOM_LLM_PARALLEL:-2}"
MMPROJ="${AXIOM_MMPROJ:-$HOME/.local/share/llm/models/mmproj-Qwen3.6-35B-A3B-F16.gguf}"
# --check: is a local model installed on this machine? (used by `./axiom worker`; a laptop without one skips it)
if [[ "${1:-}" == "--check" ]]; then
  [[ -x "$LLAMA_DIR/llama-server" && -f "$MODEL" ]]
  exit $?
fi
EXTRA=()
[ -f "$MMPROJ" ] && EXTRA+=(--mmproj "$MMPROJ")
exec "$LLAMA_DIR/llama-server" -m "$MODEL" --host 127.0.0.1 --port "$PORT" \
  -c "$CTX" -np "$PAR" --fit on -fa on --jinja --alias qwen3.6-35b-a3b-q4kxl "${EXTRA[@]}" "$@"
